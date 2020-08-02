#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from rls.utils.display import show_dict
from rls.utils.sundry_utils import check_or_create
from rls.parse.parse_buffer import get_buffer
from rls.utils.time import get_time_hhmmss
from rls.algos import get_model_info
from rls.common.train.unity import \
    unity_train, \
    unity_no_op, \
    unity_inference
from rls.common.train.gym import \
    gym_train, \
    gym_no_op, \
    gym_inference
from rls.common.yaml_ops import \
    save_config, \
    load_config
from rls.common.make_env import make_env
from rls.common.config import Config
from typing import \
    Dict, \
    NoReturn, \
    Optional
from pathlib import Path
from copy import deepcopy
import numpy as np
import os
import sys
import time

from rls.utils.logging_utils import get_logger
logger = get_logger(__name__)


def UpdateConfig(config: Dict, file_path: str, key_name: str = 'algo') -> Dict:
    '''
    update configurations from a readable file.
    params:
        config: current configurations
        file_path: path of configuration file that needs to be loaded
        key_name: a specified key in configuration file that needs to update current configurations
    return:
        config: updated configurations
    '''
    _config = load_config(file_path)
    key_values = _config[key_name]
    try:
        for key in key_values:
            config[key] = key_values[key]
    except Exception as e:
        logger.info(e)
        sys.exit()
    return config


class Trainer:
    def __init__(self, env_args: Config, apex_buffer_args: Config, train_args: Config):
        '''
        Initilize an agent that consists of training environments, algorithm model, replay buffer.
        params:
            env_args: configurations of training environments
            apex_buffer_args: configurations of replay buffer
            train_args: configurations of training
        '''
        self.env_args = env_args
        self.apex_buffer_args = apex_buffer_args
        self.train_args = train_args

        self._name = self.train_args['name']
        self.train_args['base_dir'] = os.path.join(self.train_args['base_dir'], self.train_args['name'])  # train_args['base_dir'] DIR/ENV_NAME/ALGORITHM_NAME

        self.start_time = time.time()
        self._allow_print = bool(self.train_args.get('allow_print', False))

        # ENV
        self.env = make_env(self.env_args.to_dict)

        # ALGORITHM CONFIG
        self.MODEL, self.algo_args, self.train_args['policy_mode'], _policy_type = get_model_info(self.train_args['algo'])
        self.multi_agents_training = _policy_type == 'multi'

        if self.train_args['algo_config'] is not None:
            self.algo_args = UpdateConfig(self.algo_args, self.train_args['algo_config'], 'algo')
        self.algo_args['use_rnn'] = self.train_args['use_rnn']
        show_dict(self.algo_args)

        # BUFFER
        if self.train_args['policy_mode'] == 'off-policy':
            if self.algo_args['use_rnn'] == True:
                self.apex_buffer_args['type'] = 'EpisodeER'
                self.apex_buffer_args['batch_size'] = self.algo_args.get('episode_batch_size', 0)
                self.apex_buffer_args['buffer_size'] = self.algo_args.get('episode_buffer_size', 0)

                self.apex_buffer_args['EpisodeER']['burn_in_time_step'] = self.algo_args.get('burn_in_time_step', 0)
                self.apex_buffer_args['EpisodeER']['train_time_step'] = self.algo_args.get('train_time_step', 0)
            else:
                self.apex_buffer_args['type'] = 'ER'
                self.apex_buffer_args['batch_size'] = self.algo_args.get('batch_size', 0)
                self.apex_buffer_args['buffer_size'] = self.algo_args.get('buffer_size', 0)

                _apex_buffer_args = {}
                if self.algo_args.get('use_priority', False):
                    self.apex_buffer_args['type'] = 'P' + self.apex_buffer_args['type']
                    _apex_buffer_args.update({'max_train_step': self.train_args['max_train_step']})
                if self.algo_args.get('n_step', False):
                    self.apex_buffer_args['type'] = 'Nstep' + self.apex_buffer_args['type']
                    self.algo_args['gamma'] = pow(self.algo_args['gamma'], self.apex_buffer_args['NstepPER']['n'])  # update gamma for n-step training.
                    _apex_buffer_args.update({'gamma': self.algo_args['gamma']})
                self.apex_buffer_args[self.apex_buffer_args['type']].update(_apex_buffer_args)
        else:
            self.apex_buffer_args['type'] = 'None'
            self.train_args['pre_fill_steps'] = 0  # if on-policy, prefill experience replay is no longer needed.

        if self.env_args['type'] == 'gym':
            self.initialize_gym()
        else:
            # unity
            if self.multi_agents_training:
                self.initialize_multi_unity()
            else:
                self.initialize_unity()
        pass

    def initialize_gym(self):
        # gym
        if self.train_args['use_wandb']:
            import wandb
            check_or_create(os.path.join(self.train_args.base_dir, 'wandb'))
            wandb.init(sync_tensorboard=True, name=self._name, dir=self.train_args.base_dir, project=self.train_args['wandb_project'])

        # buffer ------------------------------
        if 'Nstep' in self.apex_buffer_args['type'] or 'Episode' in self.apex_buffer_args['type']:
            self.apex_buffer_args[self.apex_buffer_args['type']]['agents_num'] = self.env_args['env_num']
        buffer = get_buffer(self.apex_buffer_args)
        # buffer ------------------------------

        # model -------------------------------
        self.algo_args.update({
            's_dim': self.env.s_dim,
            'visual_sources': self.env.visual_sources,
            'visual_resolution': self.env.visual_resolution,
            'a_dim': self.env.a_dim,
            'is_continuous': self.env.is_continuous,
            'max_train_step': self.train_args.max_train_step,
            'base_dir': self.train_args.base_dir,
            'seed': self.train_args.seed,
            'n_agents': self.env.n
        })
        self.model = self.MODEL(**self.algo_args)
        self.model.set_buffer(buffer)
        self.model.init_or_restore(self.train_args['load_model_path'])
        # model -------------------------------

        _train_info = self.model.get_init_training_info()
        self.train_args['begin_train_step'] = _train_info['train_step']
        self.train_args['begin_frame_step'] = _train_info['frame_step']
        self.train_args['begin_episode'] = _train_info['episode']
        if not self.train_args['inference']:
            records_dict = {
                'env': self.env_args.to_dict,
                'buffer': self.apex_buffer_args.to_dict,
                'train': self.train_args.to_dict,
                'algo': self.algo_args
            }
            save_config(os.path.join(self.train_args.base_dir, 'config'), records_dict)
            if self.train_args['use_wandb']:
                wandb.config.update(records_dict)

    def initialize_multi_unity(self):
        # multi agents with unity
        assert self.env.brain_num > 1, 'if using ma* algorithms, number of brains must larger than 1'

        if 'Nstep' in self.apex_buffer_args['type'] or 'Episode' in self.apex_buffer_args['type']:
            self.apex_buffer_args[self.apex_buffer_args['type']]['agents_num'] = self.env_args['env_num']
        buffer = get_buffer(self.apex_buffer_args)

        self.algo_args.update({
            's_dim': self.env.s_dim,
            'a_dim': self.env.a_dim,
            'visual_sources': self.env.visual_sources,
            'visual_resolution': self.env.visual_resolutions,
            'is_continuous': self.env.is_continuous,
            'max_train_step': self.train_args.max_train_step,
            'base_dir': self.train_args.base_dir,
            'seed': self.train_args.seed,
            'n_agents': self.env.brain_agents,
            'brain_controls': self.env.brain_controls
        })

        self.model = self.MODEL(**self.algo_args)
        self.model.set_buffer(buffer)
        self.model.init_or_restore(self.train_args['load_model_path'])

        _train_info = self.model.get_init_training_info()
        self.train_args['begin_train_step'] = _train_info['train_step']
        self.train_args['begin_frame_step'] = _train_info['frame_step']
        self.train_args['begin_episode'] = _train_info['episode']
        if not self.train_args['inference']:
            records_dict = {
                'env': self.env_args.to_dict,
                'buffer': self.apex_buffer_args.to_dict,
                'train': self.train_args.to_dict,
                'algo': self.algo_args
            }
            save_config(os.path.join(self.train_args.base_dir, 'config'), records_dict)

    def initialize_unity(self):
        # single agent with unity
        self.models = []
        for i, b in enumerate(self.env.fixed_brain_names):
            _bargs, _targs, _aargs = map(deepcopy, [self.apex_buffer_args, self.train_args, self.algo_args])
            _targs.base_dir = os.path.join(_targs.base_dir, b)
            _targs.seed += i * 10  # 0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100
            if _targs.load_model_path is not None:
                _targs.load_model_path = os.path.join(_targs.load_model_path, b)
            if 'Nstep' in _bargs['type'] or 'Episode' in _bargs['type']:
                _bargs[_bargs['type']]['agents_num'] = self.env.brain_agents[i]
            buffer = get_buffer(_bargs)

            _aargs.update({
                's_dim': self.env.s_dim[i],
                'a_dim': self.env.a_dim[i],
                'visual_sources': self.env.visual_sources[i],
                'visual_resolution': self.env.visual_resolutions[i],
                'is_continuous': self.env.is_continuous[i],
                'max_train_step': _targs.max_train_step,
                'base_dir': _targs.base_dir,
                'seed': _targs.seed,
                'n_agents': self.env.brain_agents[i],
            })
            model = self.MODEL(**_aargs)
            model.set_buffer(buffer)
            model.init_or_restore(_targs.load_model_path)
            self.models.append(model)

            if not _targs['inference']:
                records_dict = {
                    'env': self.env_args.to_dict,
                    'buffer': _bargs.to_dict,
                    'train': _targs.to_dict,
                    'algo': _aargs
                }
                save_config(os.path.join(_targs.base_dir, 'config'), records_dict)

        _train_info = self.models[0].get_init_training_info()
        self.train_args['begin_train_step'] = _train_info['train_step']
        self.train_args['begin_frame_step'] = _train_info['frame_step']
        self.train_args['begin_episode'] = _train_info['episode']

    def pwi(self, *args, out_time: bool = False) -> NoReturn:
        if self._allow_print:
            model_info = f'{self._name} '
            if out_time:
                model_info += f'T: {get_time_hhmmss(self.start_time)} '
            logger.info(''.join([model_info, *args]))
        else:
            pass

    def __call__(self) -> NoReturn:
        '''
        train
        '''
        if self.env_args['type'] == 'gym':
            try:
                gym_no_op(
                    env=self.env,
                    model=self.model,
                    pre_fill_steps=int(self.train_args['pre_fill_steps']),
                    prefill_choose=bool(self.train_args['prefill_choose'])
                )
                gym_train(
                    env=self.env,
                    model=self.model,
                    print_func=self.pwi,
                    begin_train_step=int(self.train_args['begin_train_step']),
                    begin_frame_step=int(self.train_args['begin_frame_step']),
                    begin_episode=int(self.train_args['begin_episode']),
                    render=bool(self.train_args['render']),
                    render_episode=int(self.train_args.get('render_episode', 50000)),
                    save_frequency=int(self.train_args['save_frequency']),
                    max_step_per_episode=int(self.train_args['max_step_per_episode']),
                    max_train_episode=int(self.train_args['max_train_episode']),
                    eval_while_train=bool(self.train_args['eval_while_train']),
                    max_eval_episode=int(self.train_args['max_eval_episode']),
                    off_policy_step_eval_episodes=int(self.train_args['off_policy_step_eval_episodes']),
                    off_policy_train_interval=int(self.train_args['off_policy_train_interval']),
                    policy_mode=str(self.train_args['policy_mode']),
                    moving_average_episode=int(self.train_args['moving_average_episode']),
                    add_noise2buffer=bool(self.train_args['add_noise2buffer']),
                    add_noise2buffer_episode_interval=int(self.train_args['add_noise2buffer_episode_interval']),
                    add_noise2buffer_steps=int(self.train_args['add_noise2buffer_steps']),
                    off_policy_eval_interval=int(self.train_args['off_policy_eval_interval']),
                    max_train_step=int(self.train_args['max_train_step']),
                    max_frame_step=int(self.train_args['max_frame_step'])
                )
            finally:
                self.model.close()
                self.env.close()
        else:
            if self.multi_agents_training:
                try:
                    ma_unity_no_op(
                        env=self.env,
                        model=self.model,
                        pre_fill_steps=int(self.train_args['pre_fill_steps']),
                        prefill_choose=bool(self.train_args['prefill_choose']),
                        real_done=bool(self.train_args['real_done'])
                    )
                    ma_unity_train(
                        env=self.env,
                        model=self.model,
                        print_func=self.pwi,
                        begin_train_step=int(self.train_args['begin_train_step']),
                        begin_frame_step=int(self.train_args['begin_frame_step']),
                        begin_episode=int(self.train_args['begin_episode']),
                        save_frequency=int(self.train_args['save_frequency']),
                        max_step_per_episode=int(self.train_args['max_step_per_episode']),
                        max_train_step=int(self.train_args['max_train_step']),
                        max_frame_step=int(self.train_args['max_frame_step']),
                        max_train_episode=int(self.train_args['max_train_episode']),
                        policy_mode=str(self.train_args['policy_mode']),
                        moving_average_episode=int(self.train_args['moving_average_episode']),
                        real_done=bool(self.train_args['real_done']),
                        off_policy_train_interval=int(self.train_args['off_policy_train_interval'])
                    )
                finally:
                    self.model.close()
                    self.env.close()
            else:
                try:
                    unity_no_op(
                        env=self.env,
                        models=self.models,
                        pre_fill_steps=int(self.train_args['pre_fill_steps']),
                        prefill_choose=bool(self.train_args['prefill_choose']),
                        real_done=bool(self.train_args['real_done'])
                    )
                    unity_train(
                        env=self.env,
                        models=self.models,
                        print_func=self.pwi,
                        begin_train_step=int(self.train_args['begin_train_step']),
                        begin_frame_step=int(self.train_args['begin_frame_step']),
                        begin_episode=int(self.train_args['begin_episode']),
                        save_frequency=int(self.train_args['save_frequency']),
                        max_step_per_episode=int(self.train_args['max_step_per_episode']),
                        max_train_episode=int(self.train_args['max_train_episode']),
                        policy_mode=str(self.train_args['policy_mode']),
                        moving_average_episode=int(self.train_args['moving_average_episode']),
                        add_noise2buffer=bool(self.train_args['add_noise2buffer']),
                        add_noise2buffer_episode_interval=int(self.train_args['add_noise2buffer_episode_interval']),
                        add_noise2buffer_steps=int(self.train_args['add_noise2buffer_steps']),
                        max_train_step=int(self.train_args['max_train_step']),
                        max_frame_step=int(self.train_args['max_frame_step']),
                        real_done=bool(self.train_args['real_done']),
                        off_policy_train_interval=int(self.train_args['off_policy_train_interval'])
                    )
                finally:
                    [model.close() for model in self.models]
                    self.env.close()

    def evaluate(self) -> NoReturn:
        if self.env_args['type'] == 'gym':
            try:
                gym_inference(
                    env=self.env,
                    model=self.model,
                    episodes=self.train_args['inference_episode']
                )
            finally:
                self.model.close()
                self.env.close()
        else:
            if self.multi_agents_training:
                try:
                    ma_unity_inference(
                        env=self.env,
                        model=self.model,
                        episodes=self.train_args['inference_episode']
                    )
                finally:
                    self.model.close()
                    self.env.close()
            else:
                try:
                    unity_inference(
                        env=self.env,
                        models=self.models,
                        episodes=self.train_args['inference_episode']
                    )
                finally:
                    [model.close() for model in self.models]
                    self.env.close()

    def apex(self) -> NoReturn:
        if self.train_args['policy_mode'] != 'off-policy':
            raise Exception('Ape-X only suitable for off-policy algorithms.')

        if self.train_args['apex'] == 'learner':
            from rls.distribute.apex.learner import learner
            learner(
                ip=self.train_args['apex_learner_ip'],
                port=self.train_args['apex_learner_port'],
                model=self.model
            )
            return

        if self.train_args['apex'] == 'worker':
            from rls.distribute.apex.worker import worker
            worker(
                learner_ip=self.train_args['apex_learner_ip'],
                learner_port=self.train_args['apex_learner_port'],
                buffer_ip=self.train_args['apex_buffer_ip'],
                buffer_port=self.train_args['apex_buffer_port'],
                model=self.model,
                env=self.env)
            return

        if self.train_args['apex'] == 'buffer':
            from rls.distribute.apex.buffer import buffer
            buffer(
                ip=self.train_args['apex_buffer_ip'],
                port=self.train_args['apex_buffer_port'],
                learner_ip=self.train_args['apex_learner_ip'],
                learner_port=self.train_args['apex_learner_port'],
                buffer_args=self.train_args['apex_buffer_args']
            )
            return
