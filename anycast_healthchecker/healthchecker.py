# pylint: disable=too-few-public-methods

"""A library which provides the HealthChecker class."""
from configparser import NoOptionError
import os
import sys
import logging
from queue import Queue

from anycast_healthchecker import PROGRAM_NAME
from anycast_healthchecker.servicecheck import ServiceCheck
from anycast_healthchecker.utils import (SERVICE_OPTIONS_TYPE,
                                         get_ip_prefixes_from_bird,
                                         get_ip_prefixes_from_config,
                                         get_ip_prefixes_from_config_on_exit,
                                         reconfigure_bird,
                                         write_temp_bird_conf,
                                         archive_bird_conf,
                                         AddOperation,
                                         DeleteOperation,
                                         ServiceCheckDiedError,
                                         run_custom_bird_reconfigure)


class HealthChecker:
    """Launch threads for each service check and reconfigure BIRD daemon.

    It starts a thread for each service check we have in the configuration and
    then waits for reconfiguring Bird daemon based on the results of the
    service checks.

    It uses a queue as a way to communicate with all threads. Each thread will
    add an item in the queue, which contains the IP prefix to remove from or to
    add to BIRD configuration file. When item is added we pick it up, adjust
    BIRD configuration and then reload BIRD.

    This class should be instantiated once.

    Arguments:
        config (configparger obj): A configparser object with the configuration
        bird_configuration (dict): A dictionary with Bird settings.

    Methods:
        run(): Launches checks and updates BIRD configuration based on
        the result of the check.

    """

    def __init__(self, config, bird_configuration):
        """Initialization."""
        self.log = logging.getLogger(PROGRAM_NAME)
        self.config = config
        # A queue with IP prefixes and their action to be taken based on the
        # state of health check. An item is a tuple of 3 elements:
        #     1st: name of the thread.
        #     2nd: IP prefix.
        #     3nd: IP version, either '4' or '6'.
        self.action = Queue()
        self.bird_configuration = bird_configuration
        self.log.debug(self.bird_configuration)

        # A list of service checks
        self.services = config.sections()
        self.services.remove('daemon')

        # Holds IP prefixes per IP version for which we have a service check
        self.ip_prefixes = {}
        for ip_version in self.bird_configuration:
            _ip_prefixes = get_ip_prefixes_from_config(
                self.config,
                self.services,
                ip_version)

            self.ip_prefixes[ip_version] = _ip_prefixes

        self.log.info('initialize healthchecker')

    def _update_bird_conf_file(self, operation):
        """Update BIRD configuration.

        It adds to or removes IP prefix from BIRD configuration. It also
        updates generation time stamp in the configuration file.

        Main program will exit if configuration file cant be read/written.

        Arguments:
            operation (obj): Either an AddOperation or DeleteOperation object

        Returns:
            True if BIRD configuration was updated otherwise False.

        """
        conf_updated = False
        prefixes = []
        ip_version = operation.ip_version
        config_file = self.bird_configuration[ip_version]['config_file']
        variable_name = self.bird_configuration[ip_version]['variable_name']
        changes_counter =\
            self.bird_configuration[ip_version]['changes_counter']

        try:
            prefixes = get_ip_prefixes_from_bird(config_file)
        except OSError as error:
            self.log.error("failed to open Bird configuration %s, this is a "
                           "FATAL error, thus exiting main program", error)
            sys.exit(1)

        ip_prefixes_without_check = set(prefixes).difference(
            self.ip_prefixes[ip_version])
        if ip_prefixes_without_check:
            self.log.warning("found %s IP prefixes in Bird configuration but "
                             "we aren't configured to run health checks on "
                             "them. Either someone modified the configuration "
                             "manually or something went horrible wrong. We "
                             "remove them from Bird configuration",
                             ','.join(ip_prefixes_without_check))
            # This is faster than using lambda and filter.
            # NOTE: We don't use remove method as we want to remove more than
            # occurrences of the IP prefixes without check.
            prefixes[:] = (ip for ip in prefixes
                           if ip not in ip_prefixes_without_check)
            conf_updated = True

        # Update the list of IP prefixes based on the status of health check.
        if operation.update(prefixes):
            conf_updated = True

        if not conf_updated:
            self.log.info('no updates for bird configuration')
            return conf_updated

        if self.bird_configuration[ip_version]['keep_changes']:
            archive_bird_conf(config_file, changes_counter)

        # some IP prefixes are either removed or added, create
        # configuration with new data.
        tempname = write_temp_bird_conf(
            config_file,
            variable_name,
            prefixes
        )
        try:
            os.rename(tempname, config_file)
        except OSError as error:
            self.log.critical("failed to create Bird configuration %s, this "
                              "is a FATAL error, thus exiting main program",
                              error)
            sys.exit(1)
        else:
            self.log.info("Bird configuration for IPv%s is updated",
                          ip_version)

        if len(prefixes) == 0:
            self.log.warning("Bird configuration doesn't have IP prefixes for "
                             "any of the services we monitor! It means local "
                             "node doesn't receive any traffic")

        return conf_updated

    def run(self):
        """Launch checks and triggers updates on BIRD configuration."""
        # Launch a thread for each configuration
        if not self.services:
            self.log.warning("no service checks are configured")
        else:
            self.log.info("going to launch %s threads", len(self.services))
            if self.config.has_option('daemon', 'splay_startup'):
                splay_startup = self.config.getfloat('daemon', 'splay_startup')
            else:
                splay_startup = None

            for service in self.services:
                self.log.debug("launching thread for %s", service)
                _config = {}
                for option, getter in SERVICE_OPTIONS_TYPE.items():
                    try:
                        _config[option] = getattr(self.config, getter)(service,
                                                                       option)
                    except NoOptionError:
                        pass  # for optional settings

                _thread = ServiceCheck(service, _config, self.action,
                                       splay_startup)
                _thread.start()

        # Stay running until we are stopped
        while True:
            # Fetch items from action queue
            operation = self.action.get(block=True)

            if isinstance(operation, ServiceCheckDiedError):
                self.log.critical(operation)
                self.log.critical("This is a fatal error and the only way to "
                                  "recover is to restart, thus exiting with a "
                                  "non-zero code and let systemd act by "
                                  "triggering a restart")
                sys.exit(1)

            self.log.info("returned an item from the queue for %s with IP "
                          "prefix %s and action to %s Bird configuration",
                          operation.name,
                          operation.ip_prefix,
                          operation)
            bird_updated = self._update_bird_conf_file(operation)
            self.action.task_done()
            if bird_updated:
                ip_version = operation.ip_version
                if operation.bird_reconfigure_cmd is None:
                    reconfigure_bird(
                        self.bird_configuration[ip_version]['reconfigure_cmd'])
                else:
                    run_custom_bird_reconfigure(operation)

    def on_exit(self):
        for ip_version in self.bird_configuration:
            config_file = self.bird_configuration[ip_version]['config_file']
            ip_prefixes_in_bird = get_ip_prefixes_from_bird(config_file)
            _ip_prefix_withdraw = get_ip_prefixes_from_config_on_exit(
                self.config,
                self.services,
                ip_version,
                'withdraw')
            _ip_prefix_advertise = get_ip_prefixes_from_config_on_exit(
                self.config,
                self.services,
                ip_version,
                'advertise')

            for withdraw_ip_prefix in set(ip_prefixes_in_bird).intersection(_ip_prefix_withdraw):
                _delete_operation = DeleteOperation(
                    name='on_exit',
                    ip_prefix=withdraw_ip_prefix,
                    ip_version=ip_version,
                    bird_reconfigure_timeout=(
                        self.config['daemon'].get('custom_bird_reconfigure_cmd_timeout')
                    ),
                    bird_reconfigure_cmd=(
                        self.config['daemon'].get('custom_bird_reconfigure_cmd')
                    )
                )
                self._update_bird_conf_file(_delete_operation)

            for add_ip_prefix in set(_ip_prefix_advertise).difference(ip_prefixes_in_bird):
                _add_operation = AddOperation(
                    name='on_exit',
                    ip_prefix=add_ip_prefix,
                    ip_version=ip_version,
                    bird_reconfigure_timeout=(
                        self.config['daemon'].get('custom_bird_reconfigure_cmd_timeout')
                    ),
                    bird_reconfigure_cmd=(
                        self.config['daemon'].get('custom_bird_reconfigure_cmd')
                    )
                )
                self._update_bird_conf_file(_add_operation)

