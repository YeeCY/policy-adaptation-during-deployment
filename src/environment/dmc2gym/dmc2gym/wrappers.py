from gym import core, spaces
from dm_control import suite
from dm_control import composer
from dm_env import specs
import numpy as np
import copy
from src.env import locomotion_envs


def _spec_to_box(spec):
    def extract_min_max(s):
        # (chongyi zheng) add uint8 data type
        assert s.dtype == np.float64 or s.dtype == np.float32 or s.dtype == np.uint8
        dim = np.int(np.prod(s.shape))
        if type(s) == specs.Array:
            bound = np.inf * np.ones(dim, dtype=np.float32)
            return -bound, bound
        elif type(s) == specs.BoundedArray:
            zeros = np.zeros(dim, dtype=np.uint8 if s.dtype == np.uint8 else np.float32)
            return s.minimum + zeros, s.maximum + zeros

    mins, maxs = [], []
    for s in spec:
        mn, mx = extract_min_max(s)
        mins.append(mn)
        maxs.append(mx)
    low = np.concatenate(mins, axis=0)
    high = np.concatenate(maxs, axis=0)
    assert low.shape == high.shape
    return spaces.Box(low, high, dtype=np.float32)


def _flatten_obs(obs, exclude_keys):
    obs_pieces = []
    for k, v in obs.items():
        if k not in exclude_keys:
            flat = np.array([v]) if np.isscalar(v) else v.ravel()
            obs_pieces.append(flat)
    return np.concatenate(obs_pieces, axis=0)


class DMCWrapper(core.Env):
    def __init__(
        self,
        from_pixels=False,
        height=84,
        width=84,
        camera_id=0,
        frame_skip=1,
        channels_first=True
    ):
        self._from_pixels = from_pixels
        self._height = height  # ignore this if from_pixels = False
        self._width = width  # ignore this if from_pixels = False
        self._camera_id = camera_id  # ignore this if from_pixels = False
        self._frame_skip = frame_skip  # ignore this if from_pixels = False
        self._channels_first = channels_first  # ignore this if from_pixels = False

        self._env = None

        self._true_action_space = None
        self._norm_action_space = None
        self._observation_space = None
        self._state_space = None

        self._exclude_obs_keys = None  # used for vector observation

        self.current_state = None

    def __getattr__(self, name):
        return getattr(self._env, name)

    def _get_obs(self, time_step):
        if self._from_pixels:
            obs = self.render(
                height=self._height,
                width=self._width
            )

            if self._channels_first:
                obs = obs.transpose(2, 0, 1).copy()
        else:
            obs = _flatten_obs(time_step.observation, exclude_keys=self._exclude_obs_keys)
        return obs

    def _convert_action(self, action):
        action = action.astype(np.float64)
        true_delta = self._true_action_space.high - self._true_action_space.low
        norm_delta = self._norm_action_space.high - self._norm_action_space.low
        action = (action - self._norm_action_space.low) / norm_delta
        action = action * true_delta + self._true_action_space.low
        action = action.astype(np.float32)
        return action

    @property
    def observation_space(self):
        return self._observation_space

    @property
    def state_space(self):
        return self._state_space

    @property
    def action_space(self):
        return self._norm_action_space

    @property
    def height(self):
        return self._height

    @property
    def width(self):
        return self._width

    @property
    def camera_id(self):
        return self._camera_id

    def seed(self, seed=None):
        self._true_action_space.seed(seed)
        self._norm_action_space.seed(seed)
        self._observation_space.seed(seed)

    def step(self, action):
        assert self._norm_action_space.contains(action)
        action = self._convert_action(action)
        assert self._true_action_space.contains(action)
        reward = 0
        extra = {'internal_state': self._env.physics.get_state().copy()}

        for _ in range(self._frame_skip):
            time_step = self._env.step(action)
            reward += time_step.reward or 0
            done = time_step.last()
            if done:
                break
        obs = self._get_obs(time_step)
        self.current_state = _flatten_obs(time_step.observation,
                                          exclude_keys=self._exclude_obs_keys)
        extra['discount'] = time_step.discount
        return obs, reward, done, extra

    def reset(self):
        time_step = self._env.reset()
        self.current_state = _flatten_obs(time_step.observation,
                                          exclude_keys=self._exclude_obs_keys)
        obs = self._get_obs(time_step)
        return obs

    def render(self, mode='rgb_array', height=None, width=None, camera_id=None):
        assert mode == 'rgb_array' or 'segmentation', 'only support rgb_array and segmentation mode, given %s' % mode
        img = self._env.physics.render(
            height=height if height is not None else self._height,
            width=width if width is not None else self._width,
            camera_id=camera_id if camera_id is not None else self._camera_id,
            segmentation=(mode == 'segmentation')
        )

        return img


class DMCSuiteWrapper(DMCWrapper):
    def __init__(
        self,
        domain_name,
        task_name,
        task_kwargs=None,
        visualize_reward={},
        from_pixels=False,
        height=84,
        width=84,
        camera_id=0,
        frame_skip=1,
        environment_kwargs=None,
        setting_kwargs=None,
        channels_first=True
    ):
        super(DMCSuiteWrapper, self).__init__(
            from_pixels,
            height,
            width,
            camera_id,
            frame_skip,
            channels_first
        )

        assert 'random' in task_kwargs, 'please specify a seed, for deterministic behaviour'
        self._domain_name = domain_name
        self._task_name = task_name
        self._task_kwargs = task_kwargs
        self._visualize_reward = visualize_reward
        self._environment_kwargs = environment_kwargs
        self._setting_kwargs = setting_kwargs

        # create task
        self._env = suite.load(
            domain_name=domain_name,
            task_name=task_name,
            task_kwargs=task_kwargs,
            visualize_reward=visualize_reward,
            environment_kwargs=environment_kwargs,
            setting_kwargs=setting_kwargs
        )

        # true and normalized action spaces
        self._true_action_space = _spec_to_box([self._env.action_spec()])
        self._norm_action_space = spaces.Box(
            low=-1.0,
            high=1.0,
            shape=self._true_action_space.shape,
            dtype=np.float32
        )

        # create observation space
        if from_pixels:
            shape = [3, height, width] if channels_first else [height, width, 3]
            self._observation_space = spaces.Box(
                low=0, high=255, shape=shape, dtype=np.uint8
            )
        else:
            self._observation_space = _spec_to_box(
                self._env.observation_spec().values()
            )
            
        self._state_space = _spec_to_box(
                self._env.observation_spec().values()
        )
        
        self.current_state = None

        # set seed
        self.seed(seed=task_kwargs.get('random', 1))


class DMCLocomotionWrapper(DMCWrapper):
    def __init__(
        self,
        env_name,
        task_kwargs=None,
        from_pixels=False,
        height=84,
        width=84,
        camera_id=0,
        frame_skip=1,
        channels_first=True
    ):
        super(DMCLocomotionWrapper, self).__init__(
            from_pixels,
            height,
            width,
            camera_id,
            frame_skip,
            channels_first
        )
        assert 'random' in task_kwargs, 'please specify a seed, for deterministic behaviour'
        self._task_kwargs = task_kwargs

        # create environment
        self._env = getattr(locomotion_envs, env_name)()

        # true and normalized action spaces
        self._true_action_space = _spec_to_box([self._env.action_spec()])
        self._norm_action_space = spaces.Box(
            low=-1.0,
            high=1.0,
            shape=self._true_action_space.shape,
            dtype=np.float32
        )

        # create observation space
        if from_pixels:
            shape = [3, height, width] if channels_first else [height, width, 3]
            self._observation_space = spaces.Box(
                low=0, high=255, shape=shape, dtype=np.uint8
            )
            self._state_space = _spec_to_box(
                self._env.observation_spec().values()
            )
        else:
            # TODO (chongyi zheng): remove egocentric_camera observation?
            #   Yes in the current version
            obs_spec = copy.deepcopy(self._env.observation_spec())
            keys = list(obs_spec.keys())
            for key in keys:
                if 'egocentric_camera' in key:
                    del obs_spec[key]  # 'egocentric_camera' could be a substring of 'key'
                    self._exclude_obs_keys = [key]
            self._observation_space = _spec_to_box(
                obs_spec.values()
            )

            self._state_space = _spec_to_box(
                obs_spec.values()
            )

        self.current_state = None

        # set seed
        self.seed(seed=task_kwargs.get('random', 1))
