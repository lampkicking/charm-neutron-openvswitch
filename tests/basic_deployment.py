#!/usr/bin/env python
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import amulet
import time

from charmhelpers.contrib.openstack.amulet.deployment import (
    OpenStackAmuletDeployment
)

# This file needs de-linted.  The (mis)use of n-o q-a below causes all lint
# to go undetected.  Remove that & fixme.
from charmhelpers.contrib.openstack.amulet.utils import (
    OpenStackAmuletUtils,
    DEBUG,
)

# Use DEBUG to turn on debug logging
u = OpenStackAmuletUtils(DEBUG)

# XXX Tests inspecting relation data from the perspective of the
# neutron-openvswitch are missing because amulet sentries aren't created for
# subordinates Bug#1421388


class NeutronOVSBasicDeployment(OpenStackAmuletDeployment):
    """Amulet tests on a basic neutron-openvswtich deployment."""

    def __init__(self, series, openstack=None, source=None,
                 stable=True):
        """Deploy the entire test environment."""
        super(NeutronOVSBasicDeployment, self).__init__(series, openstack,
                                                        source, stable)
        self._add_services()
        self._add_relations()
        self._configure_services()
        self._deploy()

        u.log.info('Waiting on extended status checks...')
        self.exclude_services = ['neutron-openvswitch']
        self._wait_and_check(exclude_services=self.exclude_services)
        self._initialize_tests()

    def _add_services(self):
        """Add services

           Add the services that we're testing, where neutron-openvswitch is
           local, and the rest of the service are from lp branches that are
           compatible with the local charm (e.g. stable or next).
           """
        # Services and relations which are present merely to satisfy
        # required_interfaces and workload status are not inspected.
        # Fix me. Inspect those too.
        this_service = {'name': 'neutron-openvswitch'}
        other_services = [
            {'name': 'nova-compute'},
            {'name': 'nova-cloud-controller'},
            {'name': 'rabbitmq-server'},
            {'name': 'keystone'},
            {'name': 'glance'},
            {'name': 'neutron-api'},
            {'name': 'percona-cluster', 'constraints': {'mem': '3072M'}},
        ]
        super(NeutronOVSBasicDeployment, self)._add_services(this_service,
                                                             other_services)

    def _add_relations(self):
        """Add all of the relations for the services."""
        relations = {
            'neutron-openvswitch:amqp': 'rabbitmq-server:amqp',
            'neutron-openvswitch:neutron-plugin':
            'nova-compute:neutron-plugin',
            'neutron-openvswitch:neutron-plugin-api':
            'neutron-api:neutron-plugin-api',
            # Satisfy workload stat:
            'neutron-api:identity-service': 'keystone:identity-service',
            'neutron-api:shared-db': 'percona-cluster:shared-db',
            'neutron-api:amqp': 'rabbitmq-server:amqp',
            'nova-compute:amqp': 'rabbitmq-server:amqp',
            'nova-compute:image-service': 'glance:image-service',
            'glance:identity-service': 'keystone:identity-service',
            'glance:shared-db': 'percona-cluster:shared-db',
            'glance:amqp': 'rabbitmq-server:amqp',
            'keystone:shared-db': 'percona-cluster:shared-db',
            'nova-cloud-controller:shared-db': 'percona-cluster:shared-db',
            'nova-cloud-controller:amqp': 'rabbitmq-server:amqp',
            'nova-cloud-controller:identity-service': 'keystone:'
                                                      'identity-service',
            'nova-cloud-controller:cloud-compute': 'nova-compute:'
                                                   'cloud-compute',
            'nova-cloud-controller:image-service': 'glance:image-service',
        }
        super(NeutronOVSBasicDeployment, self)._add_relations(relations)

    def _configure_services(self):
        """Configure all of the services."""
        neutron_ovs_config = {}
        neutron_ovs_config['enable-sriov'] = True
        neutron_ovs_config['sriov-device-mappings'] = 'physnet42:eth42'

        pxc_config = {
            'dataset-size': '25%',
            'max-connections': 1000,
            'root-password': 'ChangeMe123',
            'sst-password': 'ChangeMe123',
        }
        nova_cc_config = {'network-manager': 'Neutron'}
        configs = {
            'neutron-openvswitch': neutron_ovs_config,
            'percona-cluster': pxc_config,
            'nova-cloud-controller': nova_cc_config,
        }
        super(NeutronOVSBasicDeployment, self)._configure_services(configs)

    def _initialize_tests(self):
        """Perform final initialization before tests get run."""
        # Access the sentries for inspecting service units
        self.compute_sentry = self.d.sentry['nova-compute'][0]
        self.rabbitmq_sentry = self.d.sentry['rabbitmq-server'][0]
        self.neutron_api_sentry = self.d.sentry['neutron-api'][0]
        self.n_ovs_sentry = self.d.sentry['neutron-openvswitch'][0]

    def _wait_and_check(self, sleep=5, exclude_services=[]):
        """Extended wait and check helper

        The tests for neutron-openvswitch are particularly sensitive to
        timing races. This is partially due to the configuration changes being
        set against neutron-api and needing to wait for the relation to update
        neutron-openvswitch.

        This helper will attempt to mitigate these race conditions. It is
        purposefully redundant to attempt to handle the races.

        This should be called after every self.d.configure() call.

        :param sleep: Integer sleep value
        :param excluded_services: List of excluded services not to be checked
        """
        u.log.debug('Extended wait and check ...')
        time.sleep(sleep)
        self.d.sentry.wait(timeout=900)
        time.sleep(sleep)
        self._auto_wait_for_status(exclude_services=exclude_services)
        time.sleep(sleep)
        self.d.sentry.wait()
        u.log.debug('Wait and check completed.')

    def test_100_services(self):
        """Verify the expected services are running on the corresponding
           service units."""
        u.log.debug('Checking system services on units...')

        services = {
            self.compute_sentry: ['nova-compute',
                                  'neutron-plugin-openvswitch-agent'],
            self.rabbitmq_sentry: ['rabbitmq-server'],
            self.neutron_api_sentry: ['neutron-server'],
        }

        if self._get_openstack_release() >= self.trusty_mitaka:
            services[self.compute_sentry] = [
                'nova-compute',
                'neutron-openvswitch-agent'
            ]

        ret = u.validate_services_by_name(services)
        if ret:
            amulet.raise_status(amulet.FAIL, msg=ret)

        u.log.debug('OK')

    def test_301_neutron_sriov_config(self):
        """Verify data in the sriov agent config file. This is supported since
           Kilo"""
        if self._get_openstack_release() < self.trusty_kilo:
            u.log.debug('Skipping test, sriov agent not supported on < '
                        'trusty/kilo')
            return
        u.log.debug('Checking sriov agent config file data...')
        unit = self.n_ovs_sentry
        conf = '/etc/neutron/plugins/ml2/sriov_agent.ini'
        expected = {
            'sriov_nic': {
                'physical_device_mappings': 'physnet42:eth42',
            },
        }
        for section, pairs in expected.iteritems():
            ret = u.validate_config_data(unit, conf, section, pairs)
            if ret:
                message = "sriov agent config error: {}".format(ret)
                amulet.raise_status(amulet.FAIL, msg=message)

        # the CI environment does not expose an actual SR-IOV NIC to the
        # functional tests. consequently the neutron-sriov agent will not
        # run, and the charm will update its status as such. this will prevent
        # the success of pause/resume test.
        #
        # disable sriov after validation of config file is complete.
        u.log.info('Disabling SR-IOV after verifying config file data...')
        configs = {
            'neutron-openvswitch': {'enable-sriov': False}
        }
        super(NeutronOVSBasicDeployment, self)._configure_services(configs)

        u.log.info('Waiting for config-change to complete...')
        self._wait_and_check()

        u.log.debug('OK')

    def test_rabbitmq_amqp_relation(self):
        """Verify data in rabbitmq-server/neutron-openvswitch amqp relation"""
        unit = self.rabbitmq_sentry
        relation = ['amqp', 'neutron-openvswitch:amqp']
        expected = {
            'private-address': u.valid_ip,
            'password': u.not_null,
            'hostname': u.valid_ip
        }

        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('rabbitmq amqp', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_nova_compute_relation(self):
        """Verify the nova-compute to neutron-openvswitch relation data"""
        unit = self.compute_sentry
        relation = ['neutron-plugin', 'neutron-openvswitch:neutron-plugin']
        expected = {
            'private-address': u.valid_ip,
        }

        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('nova-compute neutron-plugin', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_neutron_api_relation(self):
        """Verify the neutron-api to neutron-openvswitch relation data"""
        unit = self.neutron_api_sentry
        relation = ['neutron-plugin-api',
                    'neutron-openvswitch:neutron-plugin-api']
        expected = {
            'private-address': u.valid_ip,
        }

        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('neutron-api neutron-plugin-api', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def process_ret(self, ret=None, message=None):
        if ret:
            amulet.raise_status(amulet.FAIL, msg=message)

    def check_ml2_setting_propagation(self, service, charm_key,
                                      config_file_key, vpair,
                                      section):

        # Needs love - test actions not clear in log
        unit = self.compute_sentry
        if self._get_openstack_release() >= self.trusty_mitaka:
            conf = "/etc/neutron/plugins/ml2/openvswitch_agent.ini"
        else:
            conf = "/etc/neutron/plugins/ml2/ml2_conf.ini"
        for value in vpair:
            self.d.configure(service, {charm_key: value})
            self._wait_and_check(sleep=60)
            ret = u.validate_config_data(unit, conf, section,
                                         {config_file_key: value})
            msg = "Propagation error, expected %s=%s" % (config_file_key,
                                                         value)
            self.process_ret(ret=ret, message=msg)

    def test_l2pop_propagation(self):
        """Verify that neutron-api l2pop setting propagates to neutron-ovs"""

        # Needs love - not idempotent
        self.check_ml2_setting_propagation('neutron-api',
                                           'l2-population',
                                           'l2_population',
                                           ['False', 'True'],
                                           'agent')

    def test_nettype_propagation(self):
        """Verify that neutron-api nettype setting propagates to neutron-ovs"""

        # Needs love - not idempotent
        self.check_ml2_setting_propagation('neutron-api',
                                           'overlay-network-type',
                                           'tunnel_types',
                                           ['vxlan', 'gre'],
                                           'agent')

    def test_secgroup_propagation_local_override(self):
        """Verify disable-security-groups overrides what neutron-api says"""

        # Needs love - not idempotent
        unit = self.compute_sentry
        if self._get_openstack_release() >= self.trusty_mitaka:
            conf = "/etc/neutron/plugins/ml2/openvswitch_agent.ini"
        else:
            conf = "/etc/neutron/plugins/ml2/ml2_conf.ini"
        self.d.configure('neutron-api', {'neutron-security-groups': 'True'})
        self.d.configure('neutron-openvswitch',
                         {'disable-security-groups': 'True'})
        self._wait_and_check()
        ret = u.validate_config_data(unit, conf, 'securitygroup',
                                     {'enable_security_group': 'False'})
        msg = "Propagation error, expected %s=%s" % ('enable_security_group',
                                                     'False')
        self.process_ret(ret=ret, message=msg)
        self.d.configure('neutron-openvswitch',
                         {'disable-security-groups': 'False'})
        self.d.configure('neutron-api', {'neutron-security-groups': 'True'})
        self._wait_and_check()
        ret = u.validate_config_data(unit, conf, 'securitygroup',
                                     {'enable_security_group': 'True'})

    def test_z_restart_on_config_change(self):
        """Verify that the specified services are restarted when the
        config is changed."""

        sentry = self.n_ovs_sentry
        juju_service = 'neutron-openvswitch'

        # Expected default and alternate values
        set_default = {'debug': 'False'}
        set_alternate = {'debug': 'True'}

        # Services which are expected to restart upon config change,
        # and corresponding config files affected by the change
        conf_file = '/etc/neutron/neutron.conf'
        services = {
            'neutron-openvswitch-agent': conf_file
        }

        # Make config change, check for svc restart, conf file mod time change
        u.log.debug('Making config change on {}...'.format(juju_service))
        mtime = u.get_sentry_time(sentry)
        self.d.configure(juju_service, set_alternate)
        self._wait_and_check()

        sleep_time = 30
        for s, conf_file in services.iteritems():
            u.log.debug("Checking that service restarted: {}".format(s))
            if not u.validate_service_config_changed(sentry, mtime, s,
                                                     conf_file,
                                                     sleep_time=sleep_time):
                self.d.configure(juju_service, set_default)
                self._wait_and_check()
                msg = "service {} didn't restart after config change".format(s)
                amulet.raise_status(amulet.FAIL, msg=msg)

        u.log.debug('OK')

    def test_400_enable_qos(self):
        """Check qos settings set via neutron-api charm"""
        if self._get_openstack_release() >= self.trusty_mitaka:
            unit = self.n_ovs_sentry
            set_default = {'enable-qos': 'False'}
            set_alternate = {'enable-qos': 'True'}
            self.d.configure('neutron-api', set_alternate)
            self._wait_and_check(sleep=60)
            qos_plugin = 'qos'
            config = u._get_config(
                self.neutron_api_sentry, '/etc/neutron/neutron.conf')
            service_plugins = config.get(
                'DEFAULT',
                'service_plugins').split(',')
            if qos_plugin not in service_plugins:
                message = "{} not in service_plugins".format(qos_plugin)
                amulet.raise_status(amulet.FAIL, msg=message)

            config = u._get_config(
                unit,
                '/etc/neutron/plugins/ml2/openvswitch_agent.ini')
            extensions = config.get('agent', 'extensions').split(',')
            if qos_plugin not in extensions:
                message = "qos not in extensions"
                amulet.raise_status(amulet.FAIL, msg=message)

            u.log.debug('Setting QoS back to {}'.format(
                set_default['enable-qos']))
            self.d.configure('neutron-api', set_default)
            self._wait_and_check()
            u.log.debug('OK')

    def test_910_pause_and_resume(self):
        """The services can be paused and resumed. """
        u.log.debug('Checking pause and resume actions...')
        sentry_unit = self.n_ovs_sentry

        assert u.status_get(sentry_unit)[0] == "active"

        action_id = u.run_action(sentry_unit, "pause")
        assert u.wait_on_action(action_id), "Pause action failed."
        assert u.status_get(sentry_unit)[0] == "maintenance"

        action_id = u.run_action(sentry_unit, "resume")
        assert u.wait_on_action(action_id), "Resume action failed."
        assert u.status_get(sentry_unit)[0] == "active"
        u.log.debug('OK')
