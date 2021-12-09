"""Test HLO cost model."""

import pickle
import unittest

import jax
import jax.numpy as jnp
from flax import linen as nn
from flax.training.train_state import TrainState
import optax
import ray

from parax import parallelize, set_parallelize_options, testing, PhysicalDeviceMesh, DeviceCluster, ProfilingResultDatabase
from parax.mesh_profiling import estimate_hlo_module_cost
from parax.global_env import global_config
from parax.util import map_to_shape


class HloCostModelTest(unittest.TestCase):

    def setUp(self):
        jax.config.update('jax_platform_name', 'cpu')

        # Backup global config
        self.old_global_config = global_config.backup()
        ray.init(address='auto')

    def tearDown(self):
        # Restore global config
        global_config.restore(self.old_global_config)
        ray.shutdown()

    def run_n_layer_mlp(self,
                        num_layers,
                        batch_size,
                        input_dim,
                        output_dim,
                        hidden_dim,
                        device_mesh,
                        use_bias=True):
        set_parallelize_options(devices=device_mesh)

        class Model(nn.Module):

            @nn.compact
            def __call__(self, x):
                for i in range(num_layers - 1):
                    x = nn.Dense(features=hidden_dim, use_bias=use_bias)(x)
                    x = nn.relu(x)
                x = nn.Dense(features=output_dim, use_bias=use_bias)(x)
                return x

        @parallelize
        def train_step(state, batch):

            def loss_func(params):
                out = state.apply_fn(params, batch["x"])
                return jnp.mean((out - batch["y"])**2)

            grads = jax.grad(loss_func)(state.params)
            new_state = state.apply_gradients(grads=grads)
            return new_state

        x = jnp.ones((batch_size, input_dim))
        y = jnp.ones((batch_size, output_dim))

        # Init train state
        model = Model()
        rngkey = jax.random.PRNGKey(0)
        params = model.init(rngkey, x)
        tx = optax.adam(learning_rate=1e-2)
        state = TrainState.create(apply_fn=model.apply, params=params, tx=tx)

        # JIT compile
        state = train_step(state, {"x": x, "y": y})

        # Get optimized HLO IR
        hlo_module = testing.last_compiled_executable.hlo_modules()[0]
        return hlo_module

    def test_cluster_profling(self):
        cluster = DeviceCluster()
        prof_database = cluster.profile_all("p3.16", comm_size_range=(19, 20))
        prof_database.save("tmp_prof_database.pkl")

    def test_n_layer_mlp(self):
        num_layers = 2
        batch_size = 32
        hidden_dim = 16

        prof_database = ProfilingResultDatabase()
        prof_database.load("tmp_prof_database.pkl")

        device_mesh = DeviceCluster().get_physical_mesh()
        logical_mesh = device_mesh.get_default_logical_mesh()
        hlo_module = self.run_n_layer_mlp(num_layers, batch_size, hidden_dim,
                                          hidden_dim, hidden_dim, logical_mesh)
        mesh_result = prof_database.query("p3.16", logical_mesh.shape)
        cost = estimate_hlo_module_cost(hlo_module, mesh_result)
        assert cost > 0


def suite():
    suite = unittest.TestSuite()
    suite.addTest(HloCostModelTest("test_cluster_profling"))
    suite.addTest(HloCostModelTest("test_n_layer_mlp"))
    return suite


if __name__ == "__main__":
    runner = unittest.TextTestRunner()
    runner.run(suite())