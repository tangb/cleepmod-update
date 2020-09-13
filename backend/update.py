#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import time
import random
from threading import Lock
from cleep.exception import MissingParameter, InvalidParameter, CommandError, CommandInfo
from cleep.core import CleepModule
from cleep.libs.internals.installmodule import PATH_INSTALL
from cleep.libs.configs.modulesjson import ModulesJson
from cleep.libs.configs.cleepconf import CleepConf
from cleep.libs.internals.cleepgithub import CleepGithub
import cleep.libs.internals.tools as Tools
from cleep import __version__ as VERSION
from cleep.libs.internals.installcleep import InstallCleep
from cleep.libs.internals.install import Install
from cleep.libs.internals.task import Task

class Update(CleepModule):
    """
    Update application
    """
    MODULE_AUTHOR = u'Cleep'
    MODULE_VERSION = u'1.0.0'
    MODULE_DEPS = []
    MODULE_DESCRIPTION = u'Applications and Cleep updater'
    MODULE_LONGDESCRIPTION = u'Manage all Cleep applications and Cleep core updates.'
    MODULE_TAGS = ['update', 'application', 'module']
    MODULE_CATEGORY = u'APPLICATION'
    MODULE_COUNTRY = None
    MODULE_URLINFO = u'https://github.com/tangb/cleepmod-update'
    MODULE_URLHELP = None
    MODULE_URLSITE = None
    MODULE_URLBUGS = u'https://github.com/tangb/cleepmod-update/issues'

    MODULE_CONFIG_FILE = u'update.conf'
    DEFAULT_CONFIG = {
        'cleepupdateenabled': False,
        'modulesupdateenabled': False,
        'cleepupdate': {
            'version': None,
            'changelog': None,
            'packageurl': None,
            'checksumurl': None,
        },
        'cleeplastcheck': None,
        'modulesupdate': False,
        'moduleslastcheck': None,
    }

    CLEEP_GITHUB_OWNER = u'tangb'
    CLEEP_GITHUB_REPO = u'cleep'
    PROCESS_STATUS_FILENAME = 'process.log'
    CLEEP_STATUS_FILEPATH = ''
    ACTION_MODULE_INSTALL = 'install'
    ACTION_MODULE_UPDATE = 'update'
    ACTION_MODULE_UNINSTALL = 'uninstall'

    MAIN_ACTIONS_TASK_INTERVAL = 60.0
    SUB_ACTIONS_TASK_INTERVAL = 10.0

    def __init__(self, bootstrap, debug_enabled):
        """
        Constructor

        Params:
            bootstrap (dict): bootstrap objects
            debug_enabled: debug status
        """
        CleepModule.__init__(self, bootstrap, debug_enabled)

        # members
        self.modules_json = ModulesJson(self.cleep_filesystem)
        self.cleep_conf = CleepConf(self.cleep_filesystem)
        self._modules_updates = {}
        self._check_update_time = {
            'hour': int(random.uniform(0, 24)),
            'minute': int(random.uniform(0, 60))
        }
        self.logger.debug('Software updates will be checked every day at %(hour)02d:%(minute)02d' % self._check_update_time)
        self.__processor = None
        self._need_restart = False
        # contains main actions (install/uninstall/update)
        self.__main_actions = []
        self.__main_actions_mutex = Lock()
        self.__main_actions_task = None
        # contains sub actions of mains actions (to perform action on dependencies)
        self.__sub_actions = []
        self.__sub_actions_lock = Lock()
        self.__sub_actions_task = None

        # events
        self.update_module_install = self._get_event(u'update.module.install')
        self.update_module_uninstall = self._get_event(u'update.module.uninstall')
        self.update_module_update = self._get_event(u'update.module.update')
        self.update_cleep_update = self._get_event(u'update.cleep.update')

    def _configure(self):
        """
        Configure module
        """
        # init installed modules
        self._fill_modules_updates()

        # launch main actions task if module auto update enabled
        config = self._get_config()
        if config[u'modulesupdateenabled']:
            self.update_modules()

    def event_received(self, event):
        """
        Event received

        Params:
            event (MessageRequest): event data
        """
        if event[u'event'] == u'parameters.time.now':
            # update
            if event[u'params'][u'hour'] == self._check_update_time['hour'] and event[u'params'][u'minute'] == self._check_update_time['minute']:
                # check updates
                self.check_cleep_updates()
                self.check_modules_updates()

                # and perform updates if allowed
                # update in priority cleep then modules
                config = self._get_config()
                if config[u'cleepupdateenabled']:
                    try:
                        self.update_cleep()
                    except Exception: # pragma: no cover
                        self.crash_report.report_exception()
                elif config[u'modulesupdateenabled']:
                    try:
                        self.update_modules()
                    except Exception: # pragma: no cover
                        self.crash_report.report_exception()

    def get_modules_updates(self):
        """
        Return list of modules updates

        Returns:
            
        """
        return self._modules_updates

    def _get_installed_modules_names(self):
        """
        Return installed modules names

        Returns:
            list: list of modules names
        """
        return list(self._modules_updates.keys())

    def _execute_main_action_task(self):
        """
        Function triggered regularly to process main actions (only one running at a time)
        """
        try:
            self.__main_actions_mutex.acquire()

            # check if action is already processing
            if len(self.__sub_actions) != 0:
                self.logger.debug('Main action is already processing, stop here.')
                return

            # previous main action terminated (or first one to run)
            # remove previous action if necessary
            if len(self.__main_actions) > 0 and self.__main_actions[len(self.__main_actions)-1]['processing']:
                self.__main_actions.pop()

            # is there main action to run ?
            if len(self.__main_actions) == 0:
                self.logger.debug('No more main action to execute, stop here')
                if self.__sub_actions_task:
                    self.__sub_actions_task.stop()
                return

            # compute sub actions
            action = self.__main_actions[len(self.__main_actions)-1]
            action['processing'] = True
            if action['action'] == Update.ACTION_MODULE_INSTALL:
                self._install_main_module(action['module'])
            elif action['action'] == Update.ACTION_MODULE_UNINSTALL:
                self._uninstall_main_module(action['module'], action['extra'])
            elif action['action'] == Update.ACTION_MODULE_UPDATE:
                self._update_main_module(action['module'])
            self.logger.debug('%d sub actions postponed' % len(self.__sub_actions))

            # update main action and module infos
            action['processing'] = True
            self._set_module_process(progress=0)

            # update progress step for all sub actions
            # this is done after all sub actions are stored to compute valid progress step
            try:
                progress_step = int(100 / len(self.__sub_actions))
            except ZeroDivisionError:
                progress_step = 0
            for sub_action in self.__sub_actions:
                sub_action['progressstep'] = progress_step

            # launch sub actions task
            self.__sub_actions_task = Task(
                Update.SUB_ACTIONS_TASK_INTERVAL,
                self._execute_sub_actions_task,
                self.logger,
            )
            self.__sub_actions_task.start()

        except Exception:
            self.logger.exception('Error occured executing action: %s' % action)

        finally:
            self.__main_actions_mutex.release()

    def _execute_sub_actions_task(self):
        """
        Function triggered regularly to perform sub actions
        """
        # check if sub action is being processed
        if self.__processor:
            self.logger.trace('Sub action is processing, stop here')
            return

        # no running sub action, run next one
        sub_action = self.__sub_actions.pop()

        # is last sub actions execution failed ?
        if self._is_module_process_failed():
            self.logger.trace(
                'One of previous sub action failed during "%s" module process, stop here'
                % sub_action['main']
            )
            return

        # update module process progress
        self._set_module_process(inc_progress=sub_action['progressstep'])

        # launch sub action
        if sub_action['action'] == Update.ACTION_MODULE_INSTALL:
            self._install_module(sub_action['module'], sub_action['infos'])
        elif sub_action['action'] == Update.ACTION_MODULE_UNINSTALL:
            self._uninstall_module(sub_action['module'], sub_action['infos'], sub_action['extra'])
        elif sub_action['action'] == Update.ACTION_MODULE_UPDATE:
            self._update_module(sub_action['module'], sub_action['infos'])

    def _get_processing_module_name(self):
        """
        Return processing module name

        Returns:
            string: processing module name or None if no module is processing
        """
        if len(self.__main_actions) == 0:
            return None

        action = self.__main_actions[len(self.__main_actions)-1]
        return action['module'] if action['processing'] else None

    def _set_module_process(self, progress=None, inc_progress=None, failed=None):
        """
        Set module process infos. Nothing is updated if no module is processing.

        Args:
            progress (int): set progress value to specified value (0-100)
            inc_progress (int): increase progress value with specified value
            failed (bool): action process failed if set to False
        """
        # get processing module name
        module_name = self._get_processing_module_name()
        if not module_name:
            self.logger.debug('Can\'t update module infos when no module is processing')
            return

        # add entry in module updates in case of new module install
        if module_name not in self._modules_updates:
            module_infos = self._get_module_infos_from_modules_json(module_name)
            new_module_version = module_infos['version'] if module_infos else '0.0.0'
            self._modules_updates[module_name] = self.__get_module_update_data(module_name, None, new_module_version)

        module = self._modules_updates[module_name]
        module['processing'] = True
        if progress is not None:
            module['update']['progress'] = progress
        elif inc_progress is not None:
            module['update']['progress'] += inc_progress
        if module['update']['progress'] > 100:
            module['update']['progress'] = 100
        if failed is not None:
            module['update']['failed'] = failed
            module['update']['progress'] = 100

    def _is_module_process_failed(self):
        """
        Return True if module process failed

        Returns:
            bool: True if module process failed
        """
        module_name = self._get_processing_module_name()
        if not module_name:
            self.logger.debug('Can\'t get process status while no module is processing')
            return True

        return self._modules_updates[module_name]['update']['failed']

    def _fill_modules_updates(self):
        """
        Get modules from inventory and fill useful data for updates.
        Note that only installed modules are used to fill dict

        Notes:
            modules_updates format:

                {
                    module name (string): dict returned by __get_module_update_data
                    ...
                }

        Raises:
            Exception if send command failed
        """
        # retrieve modules from inventory
        resp = self.send_command(u'get_modules', u'inventory', timeout=20)
        if not resp or resp[u'error']:
            raise Exception(u'Unable to get modules list from inventory')
        inventory_modules = resp[u'data']

        # save modules
        modules = {}
        for module_name in inventory_modules:
            # drop not installed modules
            if not inventory_modules[module_name][u'installed']:
                continue

            modules[module_name] = self.__get_module_update_data(module_name, inventory_modules[module_name]['version'])

        self._modules_updates = modules

    def __get_module_update_data(self, module_name, installed_module_version, new_module_version=None):
        """
        Get module update data

        Args:
            module_name (string): module name
            installed_module_version (string): installed module version (installed)
            new_module_version (string): new module version after update

        Returns:
            dict: module update data::

                {
                    updatable (bool): True if module is updatable
                    processing (bool): True if module has action in progress
                    name (string): module name
                    version (string): installed module version
                    update (dict): update data::
                        {
                            progress (int): progress percentage (0-100)
                            failed (bool): True if process has failed
                            version (string): update version
                            changelog (string): update changelog
                        }
                }

        """
        return {
            'updatable': False,
            'processing': False,
            'name': module_name,
            'version': installed_module_version,
            'update': {
                'progress': 0,
                'failed': False,
                'version': new_module_version,
                'changelog': None,
            },
        }

    def _restart_cleep(self, delay=10.0):
        """
        Restart cleep sending command to system module

        Args:
            delay (float): delay before restarting (default 10.0 seconds)
        """
        resp = self.send_command(u'restart', u'system', {'delay': delay})
        if not resp or resp['error']:
            self.logger.error('Unable to restart Cleep')

    def check_cleep_updates(self):
        """
        Check for available cleep updates

        Notes:
            If GITHUB_TOKEN is referenced in env vars, it will also check pre-releases

        Returns:
            dict: last update infos::

                {
                    cleeplastcheck (int): last cleep update check timestamp
                    cleepupdate (dict): latest cleep update informations::

                        {
                            version (string): latest update version
                            changelog (string): latest update changelog
                            packageurl (string): latest update package url
                            checksumurl (string): latest update checksum url
                        }

                }

        """
        # init
        update = {
            'version': None,
            'changelog': None,
            'packageurl': None,
            'checksumurl': None
        }

        try:
            # get beta release if GITHUB_TOKEN env variable registered
            auth_string = None
            only_released = True
            if u'GITHUB_TOKEN' in os.environ:
                auth_string = 'token %s' % os.environ[u'GITHUB_TOKEN']
                only_released = False # used to get beta release

            github = CleepGithub(auth_string)
            releases = github.get_releases(
                self.CLEEP_GITHUB_OWNER,
                self.CLEEP_GITHUB_REPO,
                only_latest=True,
                only_released=only_released
            )
            if len(releases) > 0:
                # get latest version available
                latest_version = github.get_release_version(releases[0])
                latest_changelog = github.get_release_changelog(releases[0])
                self.logger.debug(u'Found latest update: %s - %s' % (latest_version, latest_changelog))

                self.logger.info('Cleep version status: latest %s - installed %s' % (latest_version, VERSION))
                if Tools.compare_versions(VERSION, latest_version):
                    # new version available, trigger update
                    assets = github.get_release_assets_infos(releases[0])
                    self.logger.trace('assets: %s' % assets)

                    # search for deb file
                    package_asset = None
                    for asset in assets:
                        if asset[u'name'].startswith(u'cleep_') and asset[u'name'].endswith('.zip'):
                            self.logger.debug(u'Found Cleep package asset: %s' % asset)
                            package_asset = asset
                            update[u'packageurl'] = asset['url']
                            break

                    # search for checksum file
                    if package_asset:
                        package_name = os.path.splitext(package_asset[u'name'])[0]
                        checksum_name = u'%s.%s' % (package_name, u'sha256')
                        self.logger.debug(u'Checksum filename to search: %s' % checksum_name)
                        for asset in assets:
                            if asset[u'name'] == checksum_name:
                                self.logger.debug(u'Found checksum asset: %s' % asset)
                                update[u'checksumurl'] = asset['url']
                                break

                    if update[u'packageurl'] and update[u'checksumurl']:
                        update['version'] = latest_version
                        update['changelog'] = latest_changelog
                    else:
                        self.logger.warning('Cleep update is available but is was impossible to retrieve all needed data')
                        update['packageurl'] = None
                        update['checksumurl'] = None

                else:
                    # already up-to-date
                    self.logger.info('Cleep is already up-to-date')

            else:
                # no release found
                self.logger.warning(u'No Cleep release found during check')

        except:
            self.logger.exception(u'Error occured during updates checking:')
            self.crash_report.report_exception()
            raise CommandError(u'Error occured during cleep update check')

        # update config
        config = {
            u'cleepupdate': update,
            u'cleeplastcheck': int(time.time())
        }
        self._update_config(config)

        return config

    def check_modules_updates(self):
        """
        Check for modules updates.

        Returns:
            dict: last modules update infos::

                {
                    modulesupdates (bool): True if at least one module has an update
                    moduleslastcheck (int): last modules update check timestamp
                    modulesjsonupdated (bool): True if modules.json updated (front needs to force modules update)
                }

        """
        # store local modules list (from modules.json)
        current_modules_json = self.modules_json.get_json()

        # update modules.json content
        try:
            modules_json_updated = self.modules_json.update()
            new_modules_json = current_modules_json
            if modules_json_updated:
                new_modules_json = self.modules_json.get_json()
        except:
            self.logger.warning('Unable to refresh modules list from repository')
            raise CommandError('Unable to refresh modules list from internet')

        # check for modules updates available
        update_available = False
        if modules_json_updated:
            for module_name, module in self._modules_updates.items():
                try:
                    new_version = new_modules_json['list'][module_name]['version'] if module_name in new_modules_json['list'] else '0.0.0'
                    if Tools.compare_versions(module['version'], new_version):
                        # new version available for current module
                        update_available = True
                        module['updatable'] = True
                        module['update']['version'] = new_version
                        module['update']['changelog'] = new_modules_json['list'][module_name]['changelog']
                        self.logger.info('New version available for app "%s" (v%s => v%s)' % (
                            module_name,
                            module['version'],
                            new_version
                        ))
                    else:
                        self.logger.debug('No new version available for app "%s" (v%s => v%s)' % (
                            module_name,
                            module['version'],
                            new_version
                        ))

                except Exception:
                    self.logger.exception('Invalid "%s" app infos from modules.json' % module_name)

        # update config
        config = {
            u'modulesupdates': update_available,
            u'moduleslastcheck': int(time.time())
        }
        self._update_config(config)

        return {
            u'modulesupdates': update_available,
            u'modulesjsonupdated': modules_json_updated,
            u'moduleslastcheck': config[u'moduleslastcheck']
        }

    def _update_cleep_callback(self, status):
        """
        Cleep update callback
        Args:
            status (dict): update status
        """
        self.logger.debug(u'Cleep update callback status: %s' % status)

        # send process status (only status)
        self.update_cleep_update.send(params={u'status': status[u'status']})

        # store final status when update terminated (successfully or not)
        if status[u'status'] >= InstallCleep.STATUS_UPDATED:
            self._store_process_status(status)

            # reset cleep update config
            self._update_config({
                'cleepupdate': {
                    'version': None,
                    'changelog': None,
                    'packageurl': None,
                    'checksumurl': None,
                }
            })

            # lock filesystem
            self.cleep_filesystem.disable_write(True, True)

        # handle end of cleep update
        if status[u'status'] == InstallCleep.STATUS_UPDATED:
            # update successful
            self.logger.info('Cleep update installed successfully. Restart now')
            self._restart_cleep()
        elif status[u'status'] > InstallCleep.STATUS_UPDATED:
            # error occured
            self.logger.error('Cleep update failed. Please check process outpout')

    def update_cleep(self):
        """
        Update Cleep installing debian package
        """
        # check
        cleep_update = self._get_config_field('cleepupdate')
        if cleep_update['version'] is None:
            raise CommandInfo('No Cleep update available, please launch update check')

        # unlock filesystem
        self.cleep_filesystem.enable_write(True, True)

        # launch update
        package_url = cleep_update[u'packageurl']
        checksum_url = cleep_update[u'checksumurl']
        self.logger.debug(u'Update Cleep: package_url=%s checksum_url=%s' % (package_url, checksum_url))
        update = InstallCleep(self.cleep_filesystem, self.crash_report)
        update.install(package_url, checksum_url, self._update_cleep_callback)

    def update_modules(self):
        """
        Update modules that can be updated. It consists of processing postponed main actions filled
        during module updates check.
        """
        if not self.__main_actions_task:
            self.__main_actions_task = Task(
                Update.MAIN_ACTIONS_TASK_INTERVAL,
                self._execute_main_action_task,
                logger=self.logger,
            )
            self.__main_actions_task.start()

    def _postpone_main_action(self, action, module_name, extra=None):
        """
        Postpone main action (module install/update/uninstall) in a FIFO list.

        Args:
            action (string): action name (see ACTION_XXX constants)
            module_name (string): module name concerned by action
            extra (any): extra data to send to aaction

        Returns:
            bool: True if new action postponed, False if action was already postponed
        """
        try:
            self.__main_actions_mutex.acquire()

            # search if similar action for same module already exists
            existing_actions = [action_obj for action_obj in self.__main_actions if action_obj['module'] == module_name and action_obj['action'] == action]
            self.logger.trace('existing_actions: %s' % existing_actions)

            if len(existing_actions) == 0:
                self.__main_actions.insert(0, {
                    'action': action,
                    'module': module_name,
                    'extra': extra,
                    'processing': False,
                })
                return True

        finally:
            self.__main_actions_mutex.release()

        self.logger.debug('Same action "%s" for "%s" module already exists, drop it' % (action, module_name))
        return False

    def _postpone_sub_action(self, action, module_name, module_infos, main_module_name, extra=None):
        """
        Postpone sub action (module install/update/uninstall) in a stand alone list.

        Args:
            action (string): action name (see ACTION_XXX constants)
            module_name (string): module name concerned by action
            module_infos (dict): module informations
            main_module_name (string): main module name
            extra (any): any extra data
        """
        self.__sub_actions.insert(0, {
            'action': action,
            'module': module_name,
            'main': main_module_name,
            'infos': module_infos,
            'extra': extra,
            'progressstep': None, # will be set after all sub actions are computed
        })

    def set_automatic_update(self, cleep_update_enabled, modules_update_enabled):
        """
        Set automatic update values

        Args:
            cleep_update_enabled (bool): enable cleep automatic update
            modules_update_enabled (bool): enable modules automatic update

        Returns:
            dict: update flags::

                {
                    cleepupdateenabled (bool): True if cleep update enabled
                    modulesupdateenabled (bool): True if module update enabled
                }

        """
        if not isinstance(cleep_update_enabled, bool):
            raise InvalidParameter('Parameter "cleep_update_enabled" is invalid')
        if not isinstance(modules_update_enabled, bool):
            raise InvalidParameter('Parameter "modules_update_enabled" is invalid')

        # stop modules update task if necessary
        if not modules_update_enabled and self.__main_actions_task:
            self.__main_actions_task.stop()

        return self._update_config({
            u'cleepupdateenabled': cleep_update_enabled,
            u'modulesupdateenabled': modules_update_enabled
        })

    def _get_module_infos_from_modules_json(self, module_name):
        """
        Return modules infos from modules.json file

        Args:
            module_name (string): module name

        Returns:
            dict: module infos

        Raises:
            Exception if modules.json is invalid
        """
        modules_json = self.modules_json.get_json()
        if module_name in modules_json['list']:
            return modules_json['list'][module_name]

        return None

    def _get_module_infos_from_inventory(self, module_name):
        """
        Return module infos from modules.json file

        Args:
            module_name (string): module name

        Returns:
            dict: module infos

        Raises:
            Exception if unknown module or error
        """
        # get infos from inventory
        resp = self.send_command('get_module_infos', u'inventory', {'module': module_name})
        if resp['error']:
            self.logger.error(u'Unable to get module "%s" infos: %s' % (module_name, resp[u'msg']))
            raise Exception('Unable to get module "%s" infos' % module_name)
        if resp['data'] is None:
            self.logger.error(u'Module "%s" not found in modules list' % module_name)
            raise Exception(u'Module "%s" not found in installable modules list' % module_name)

        return resp[u'data']

    def _get_module_dependencies(self, module_name, modules_infos, get_module_infos_callback, context=None):
        """
        Get module dependencies. Specified module will be returned in result.
        Returned items are ordered by descent with first item as deepest leaf.

        Args:
            module_name (string): module name
            module_infos (dict): module infos (as returned by _get_module_infos). It must contains
                                 infos of module_name to allow dependencies search.
            get_module_infos_callback (function): callback to get module infos. Can be either _get_module_infos_from_inventory
                                 or _get_module_infos_from_modules_json
            context (None): internal context for recursive call. Do not set.

        Returns:
            list: list of dependencies
            dict: input parameter modules_infos is also updated
        """
        if context is None:
            # initiate recursive process
            context = {
                'dependencies': [],
                'visited': [module_name],
            }
        elif module_name in context['visited']:
            # avoid circular deps
            return None

        # get module infos
        if module_name not in modules_infos:
            infos = get_module_infos_callback(module_name)
            modules_infos[module_name] = infos

        # get dependencies (recursive call)
        for dependency_name in infos[u'deps']:
            if dependency_name == module_name:
                # avoid infinite loop
                continue
            self._get_module_dependencies(dependency_name, modules_infos, get_module_infos_callback, context)

        context['dependencies'].append(module_name)
        return context['dependencies']

    def _store_process_status(self, status):
        """
        Store last module process status in filesystem

        Args:
            status (dict): process status

        """
        # if no module name specified in status, it means it's cleep process
        module_name = 'cleep'
        if 'module' in status:
            module_name = status['module']

        # build and check path
        fullpath = os.path.join(PATH_INSTALL, module_name, self.PROCESS_STATUS_FILENAME)
        path = os.path.join(PATH_INSTALL, module_name)
        if not os.path.exists(path):
            self.cleep_filesystem.mkdir(path, True)

        # store status
        if not self.cleep_filesystem.write_json(fullpath, status):
            self.logger.error('Error storing module "%s" process status into "%s"' % (module_name, fullpath))

    def __install_module_callback(self, status):
        """
        Module install callback

        Args:
            status (dict): process status::

                {
                    stdout (list): stdout output
                    stderr (list): stderr output
                    status (int): install status
                    module (string): module name
                }

        """
        self.logger.debug(u'Module install callback status: %s' % status)

        # send process status
        self.update_module_install.send(params=status)

        # save last module processing
        self._store_process_status(status)

        # handle install success
        if status[u'status'] == Install.STATUS_DONE:
            # need to restart
            self._need_restart = True

            # update cleep.conf
            self.cleep_conf.install_module(status[u'module'])
        elif status['status'] == Install.STATUS_ERROR:
            # set main action failed
            self._set_module_process(failed=True)

        # handle end of install to finalize install
        if status[u'status'] >= Install.STATUS_DONE:
            # reset processor
            self.__processor = None

    def _install_module(self, module_name, module_infos):
        """
        Execute specified module installation

        Args:
            module_name (string): module name
            module_infos (dict): module infos
        """
        # non blocking, end of process handled in specified callback
        self.__processor = Install(self.cleep_filesystem, self.crash_report, self.__install_module_callback)
        self.__processor.install_module(module_name, module_infos)

    def _install_main_module(self, module_name):
        """
        Install main module. This function will install all dependencies and update modules
        if necessary.

        Args:
            module_name (string): module name to install
        """
        installed_modules = self._get_installed_modules_names()

        # compute dependencies to install
        modules_infos_json = {}
        dependencies = self._get_module_dependencies(module_name, modules_infos_json, self._get_module_infos_from_modules_json)
        self.logger.debug(u'Module "%s" dependencies: %s' % (module_name, dependencies))

        # schedule module + dependencies installs
        for dependency_name in dependencies:
            if dependency_name not in installed_modules:
                # install dependency
                self._postpone_sub_action(
                    Update.ACTION_MODULE_INSTALL,
                    dependency_name,
                    modules_infos_json[dependency_name],
                    module_name,
                )

            else:
                # check if already installed module need to be updated
                module_infos_inventory = self._get_module_infos_from_inventory(dependency_name)
                if Tools.compare_versions(module_infos_inventory['version'], modules_infos_json[dependency_name]['version']):
                    self._postpone_sub_action(
                        Update.ACTION_MODULE_UPDATE,
                        dependency_name,
                        modules_infos_json[dependency_name],
                        module_name,
                    )

    def install_module(self, module_name):
        """
        Install specified module

        Args:
            module_name (string): module name to install
        """
        # check params
        if module_name is None or len(module_name) == 0:
            raise MissingParameter(u'Parameter "module_name" is missing')
        installed_modules = self._get_installed_modules_names()
        if module_name in installed_modules:
            raise InvalidParameter('Module "%s" is already installed' % module_name)

        # postpone module installation
        return self._postpone_main_action(
            Update.ACTION_MODULE_INSTALL,
            module_name
        )

    def __uninstall_module_callback(self, status):
        """
        Module uninstall callback

        Args:
            status (dict): process status::

                {
                    stdout (list): stdout output
                    stderr (list): stderr output
                    status (int): install status
                    module (string): module name
                }

        """
        self.logger.debug(u'Module uninstall callback status: %s' % status)

        # send process status to ui
        self.update_module_uninstall.send(params=status)

        # save last module processing
        self._store_process_status(status)

        # handle process success
        if status[u'status'] == Install.STATUS_DONE:
            self._need_restart = True

            # update cleep.conf
            self.cleep_conf.uninstall_module(status[u'module'])
        elif status['status'] == Install.STATUS_ERROR:
            # set main action failed
            self._set_module_process(failed=True)

        # handle end of process
        if status['status'] >= Install.STATUS_DONE:
            # reset processor
            self.__processor = None

    def _uninstall_module(self, module_name, module_infos, extra):
        """
        Execute specified module uninstallation

        Args:
            module_name (string): module name
            module_infos (dict): module infos
            extra (any): extra data (not used here)
        """
        self.__processor = Install(self.cleep_filesystem, self.crash_report, self.__uninstall_module_callback)
        self.__processor.uninstall_module(module_name, module_infos, extra['force'])

    def _uninstall_main_module(self, module_name, extra):
        """
        Uninstall module. This function will uninstall useless dependencies.

        Args:
            module_name (string): module name
            extra (any): extra data
        """
        # compute dependencies to uninstall
        modules_infos = {}
        dependencies = self._get_module_dependencies(module_name, modules_infos, self._get_module_infos_from_inventory)
        self.logger.debug(u'Module "%s" dependencies: %s' % (module_name, dependencies))
        modules_to_uninstall = self._get_modules_to_uninstall(dependencies, modules_infos)
        self.logger.info(u'Module "%s" uninstallation will remove "%s"' % (module_name, modules_to_uninstall))

        # schedule module + dependencies uninstalls
        for module_to_uninstall in modules_to_uninstall:
            self._postpone_sub_action(
                Update.ACTION_MODULE_UNINSTALL,
                module_to_uninstall,
                modules_infos[module_to_uninstall],
                module_name,
                extra,
            )

    def uninstall_module(self, module_name, force=False):
        """
        Uninstall specified module

        Args:
            module_name (string): module name to uninstall
            force (bool): True to force uninstall even if error occured
        """
        # check params
        if module_name is None or len(module_name) == 0:
            raise MissingParameter(u'Parameter "module_name" is missing')
        installed_modules = self._get_installed_modules_names()
        if module_name not in installed_modules:
            raise InvalidParameter('Module "%s" is not installed' % module_name)

        # postpone uninstall
        return self._postpone_main_action(
            Update.ACTION_MODULE_UNINSTALL,
            module_name,
            extra={'force': force},
        )

    def _get_modules_to_uninstall(self, modules_to_uninstall, modules_infos):
        """
        Look for modules to uninstall list and remove modules that cannot be removed
        due to dependency with other module still needed.

        Args:
            modules_to_uninstall (list): module names to uninstall
            modules_infos (dict): dict of modules infos

        Returns:
            list: modules names to uninstall
        """
        out = modules_to_uninstall[:]

        for module_to_uninstall in modules_to_uninstall:
            if module_to_uninstall not in modules_infos:
                self.logger.warning('Module infos dict should contains "%s" module infos. Module won\'t be removed and can become an orphan.' % module_to_uninstall)
                module_infos = {
                    'loadedby': ['orphan']
                }
            else:
                module_infos = modules_infos[module_to_uninstall]
            for loaded_by in module_infos[u'loadedby']:
                if loaded_by not in modules_to_uninstall:
                    # do not uninstall this module because it is a dependency of another module
                    self.logger.debug('Do not uninstall module "%s" which is still needed by "%s" module' % (
                        module_to_uninstall,
                        loaded_by
                    ))
                    out.remove(module_to_uninstall)
                    break

        return out

    def __update_module_callback(self, status):
        """
        Module update callback

        Args:
            status (dict): process status::

                {
                    stdout (list): stdout output
                    stderr (list): stderr output
                    status (int): install status
                    module (string): module name
                }

        """
        self.logger.debug(u'Module update callback status: %s' % status)

        # send process status to ui
        self.update_module_update.send(params=status)

        # save last module processing
        self._store_process_status(status)

        # handle process success
        if status[u'status'] == Install.STATUS_DONE:
            self._need_restart = True

            # update cleep.conf adding module to updated ones
            self.cleep_conf.update_module(status[u'module'])
        elif status['status'] == Install.STATUS_ERROR:
            # set main action failed
            self._set_module_process(failed=True)

        # handle end of process
        if status['status'] >= Install.STATUS_DONE:
            # reset processor
            self.__processor = None

    def _update_module(self, module_name, module_infos):
        """
        Execute module update

        Params:
            module_name (string): module name to install
            module_infos (dict): module infos
        """
        self.__processor = Install(self.cleep_filesystem, self.crash_report, self.__update_module_callback)
        self.__processor.update_module(module_name, module_infos)

    def _update_main_module(self, module_name):
        """
        Update main module performing:
            - install of new dependencies
            - uninstall of old dependencies
            - update of modules

        Args:
            module_name (string): module name
        """
        # compute module dependencies
        modules_infos_inventory = {}
        modules_infos_json = {}
        old_dependencies = self._get_module_dependencies(
            module_name,
            modules_infos_inventory,
            self._get_module_infos_from_inventory
        )
        self.logger.debug('Module "%s" old dependencies: %s' % (module_name, old_dependencies))
        new_dependencies = self._get_module_dependencies(
            module_name,
            modules_infos_json,
            self._get_module_infos_from_modules_json
        )
        self.logger.debug('Module "%s" new dependencies: %s' % (module_name, new_dependencies))
        dependencies_to_uninstall = [mod_name for mod_name in old_dependencies if mod_name not in new_dependencies]
        self.logger.debug('Module "%s" requires to uninstall modules: %s' % (module_name, dependencies_to_uninstall))
        dependencies_to_install = [mod_name for mod_name in new_dependencies if mod_name not in old_dependencies]
        self.logger.debug('Module "%s" requires to install new modules: %s' % (module_name, dependencies_to_install))
        dependencies_to_update = [
            mod_name for mod_name in new_dependencies
            if mod_name in old_dependencies
            and Tools.compare_versions(modules_infos_inventory[mod_name]['version'], modules_infos_json[mod_name]['version'])
        ]
        self.logger.debug('Module "%s" requires to update modules: %s' % (module_name, dependencies_to_update))

        # postpone old dependencies uninstallations
        for mod_name in dependencies_to_uninstall:
            self._postpone_sub_action(
                Update.ACTION_MODULE_UNINSTALL,
                mod_name,
                modules_infos_inventory[mod_name],
                module_name,
                extra={'force': True}, # always force to make sure module is completely uninstalled
            )

        # postpone new dependencies installations
        for mod_name in dependencies_to_install:
            self._postpone_sub_action(
                Update.ACTION_MODULE_INSTALL,
                mod_name,
                modules_infos_json[mod_name],
                module_name,
            )

        # postpone dependencies update
        for mod_name in dependencies_to_update:
            self._postpone_sub_action(
                Update.ACTION_MODULE_UPDATE,
                mod_name,
                modules_infos_json[mod_name],
                module_name,
            )

    def update_module(self, module_name):
        """
        Update specified module

        Args:
            module_name (string): module name to uninstall

        Returns:
            bool: True if module update started. False if update is postponed
        """
        # check params
        if module_name is None or len(module_name) == 0:
            raise MissingParameter(u'Parameter "module_name" is missing')
        installed_modules = self._get_installed_modules_names()
        if module_name not in installed_modules:
            raise InvalidParameter('Module "%s" is not installed' % module_name)

        # postpone uninstall
        return self._postpone_main_action(
            Update.ACTION_MODULE_UPDATE,
            module_name,
        )
