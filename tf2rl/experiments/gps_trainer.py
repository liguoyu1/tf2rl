import numpy as np
import tensorflow as tf
from cpprb import ReplayBuffer
from tensorflow.keras.layers import Dense

from tf2rl.algos.ilqg import ILQG
from tf2rl.experiments.trainer import Trainer
from tf2rl.misc.get_replay_buffer import get_space_size


class GPSController(tf.keras.Model):
    def __init__(self, input_dim, output_dim, units=[32, 32], name="GPS", gpu=0):
        self.device = "/gpu:{}".format(gpu) if gpu >= 0 else "/cpu:0"
        self.policy_name = "GPS"
        super().__init__(name=name)

        self.l1 = Dense(units[0], name="L1", activation="relu")
        self.l2 = Dense(units[1], name="L2", activation="relu")
        self.l3 = Dense(output_dim, name="L3", activation="linear")

        with tf.device(self.device):
            self(tf.constant(np.zeros(shape=(1, input_dim), dtype=np.float32)))

    @tf.function
    def call(self, inputs):
        features = self.l1(inputs)
        features = self.l2(features)
        return self.l3(features)

    def predict(self, inputs):
        assert isinstance(inputs, np.ndarray)
        if inputs.ndim == 1:
            inputs = np.expand_dims(inputs, axis=0)

        with tf.device(self.device):
            outputs = self.call(inputs)

        if inputs.shape[0] == 1:
            return outputs.numpy()[0]
        else:
            return outputs.numpy()


class GPSTrainer(Trainer):
    def __init__(
            self,
            make_env,
            args,
            buffer_size=int(1e6),
            lr=0.001,
            **kwargs):
        env = make_env()
        # GPS controller
        policy = GPSController(input_dim=env.observation_space.shape[0], output_dim=env.action_space.shape[0])
        self._optimizer = tf.keras.optimizers.Adam(learning_rate=lr)

        # Local controller
        self._ilqg = ILQG(self._make_env, horizon=self._horizon)

        super().__init__(policy, make_env(), args, **kwargs)

        self._make_env = make_env

        # Prepare buffer that stores transitions (s, a, s')
        rb_dict = {
            "size": buffer_size,
            "default_dtype": np.float32,
            "env_dict": {
                "obs": {
                    "shape": get_space_size(self._env.observation_space)},
                "act": {
                    "shape": get_space_size(self._env.action_space)}}}
        self.gps_buffer = ReplayBuffer(**rb_dict)

    def __call__(self):
        total_steps = 0
        tf.summary.experimental.set_step(total_steps)
        # Gather dataset of random trajectories
        self.logger.info("Ramdomly collect {} samples...".format(self._n_random_rollout * self._episode_max_steps))

        for i in range(self._max_iter):
            # Collect local optimal actions using iLQG
            self.collect_local_optimal_actions_ilqg()

            # Train GPS (global) controller
            self.fit_gps_controller(i)

    def _set_from_args(self, args):
        super()._set_from_args(args)
        self._horizon = args.horizon

    def collect_local_optimal_actions_ilqg(self):
        for i in range(self._n_random_rollout):
            self._ilqg.optimize()
            self.gps_buffer.add(obs=np.array(self._ilqg.X[:-1], dtype=np.float32),
                                act=np.array(self._ilqg.U, dtype=np.float32))
            self._logger.info("Iter {}: cost = {:.5f}".format(i + 1, self._ilqg.cost))

    @tf.function
    def _fit_gps_controller_body(self, inputs, labels):
        with tf.GradientTape() as tape:
            predicts = self._policy(inputs)
            loss = tf.reduce_mean(0.5 * tf.square(labels - predicts))
        grads = tape.gradient(
            loss, self._policy.trainable_variables)
        self._optimizer.apply_gradients(
            zip(grads, self._policy.trainable_variables))
        return loss

    def fit_gps_controller(self, n_iter, n_epoch=1):
        samples = self.gps_buffer.sample(
            self.gps_buffer.get_stored_size())
        dataset = tf.data.Dataset.from_tensor_slices((samples["obs"], samples["act"]))
        dataset = dataset.batch(self._batch_size)
        dataset = dataset.shuffle(buffer_size=1000)
        dataset = dataset.repeat(n_epoch)
        for batch, (x, y) in enumerate(dataset):
            loss = self._fit_gps_controller_body(x, y)
            self.logger.debug("batch: {} loss: {:2.6f}".format(batch, loss))
        tf.summary.scalar("gps/regression_loss", loss)
        self.logger.info("iter={0: 3d} loss: {1:2.8f}".format(n_iter, loss))

    @staticmethod
    def get_argument(parser=None):
        parser = Trainer.get_argument(parser)
        parser.add_argument('--gpu', type=int, default=0,
                            help='GPU id')
        parser.add_argument("--horizon", type=int, default=50)
        return parser