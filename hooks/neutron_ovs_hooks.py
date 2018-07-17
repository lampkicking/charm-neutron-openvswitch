#!/usr/bin/env python3
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

import sys

from copy import deepcopy

from charmhelpers.contrib.openstack.utils import (
    pausable_restart_on_change as restart_on_change,
)

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    log,
    relation_set,
    relation_ids,
)

from neutron_ovs_utils import (
    DHCP_PACKAGES,
    DVR_PACKAGES,
    METADATA_PACKAGES,
    OVS_DEFAULT,
    configure_ovs,
    configure_sriov,
    get_shared_secret,
    register_configs,
    restart_map,
    use_dvr,
    enable_nova_metadata,
    enable_local_dhcp,
    install_packages,
    purge_packages,
    assess_status,
)

hooks = Hooks()
CONFIGS = register_configs()


@hooks.hook()
def install():
    install_packages()


# NOTE(wolsen): Do NOT add restart_on_change decorator without consideration
# for the implications of modifications to the /etc/default/openvswitch-switch.
@hooks.hook('upgrade-charm')
def upgrade_charm():
    if OVS_DEFAULT in restart_map():
        # In the 16.10 release of the charms, the code changed from managing
        # the /etc/default/openvswitch-switch file only when dpdk was enabled
        # to always managing this file. Thus, an upgrade of the charm from a
        # release prior to 16.10 or higher will always cause the contents of
        # the file to change and will trigger a restart of the
        # openvswitch-switch service, which in turn causes a temporary
        # network outage. To prevent this outage, determine if the
        # /etc/default/openvswitch-switch file needs to be migrated and if
        # so, migrate the file but do NOT restart the openvswitch-switch
        # service.
        # See bug LP #1712444
        with open(OVS_DEFAULT, 'r') as f:
            # The 'Service restart triggered ...' line was added to the
            # OVS_DEFAULT template in the 16.10 version of the charm to allow
            # restarts so we use this as the key to see if the file needs
            # migrating.
            if 'Service restart triggered' not in f.read():
                CONFIGS.write(OVS_DEFAULT)


@hooks.hook('neutron-plugin-relation-changed')
@hooks.hook('config-changed')
@restart_on_change(restart_map())
def config_changed():
    install_packages()

    configure_ovs()
    CONFIGS.write_all()
    # NOTE(fnordahl): configure_sriov must be run after CONFIGS.write_all()
    # to allow us to enable boot time execution of init script
    configure_sriov()
    for rid in relation_ids('neutron-plugin'):
        neutron_plugin_joined(relation_id=rid)


@hooks.hook('neutron-plugin-api-relation-changed')
@restart_on_change(restart_map())
def neutron_plugin_api_changed():
    if use_dvr():
        install_packages()
    else:
        purge_packages(DVR_PACKAGES)
    configure_ovs()
    CONFIGS.write_all()
    # If dvr setting has changed, need to pass that on
    for rid in relation_ids('neutron-plugin'):
        neutron_plugin_joined(relation_id=rid)


@hooks.hook('neutron-plugin-relation-joined')
def neutron_plugin_joined(relation_id=None):
    if enable_local_dhcp():
        install_packages()
    else:
        pkgs = deepcopy(DHCP_PACKAGES)
        # NOTE: only purge metadata packages if dvr is not
        #       in use as this will remove the l3 agent
        #       see https://pad.lv/1515008
        if not use_dvr():
            pkgs.extend(METADATA_PACKAGES)
        purge_packages(pkgs)
    secret = get_shared_secret() if enable_nova_metadata() else None
    rel_data = {
        'metadata-shared-secret': secret,
    }
    relation_set(relation_id=relation_id, **rel_data)


@hooks.hook('amqp-relation-joined')
def amqp_joined(relation_id=None):
    relation_set(relation_id=relation_id,
                 username=config('rabbit-user'),
                 vhost=config('rabbit-vhost'))


@hooks.hook('amqp-relation-changed')
@hooks.hook('amqp-relation-departed')
@restart_on_change(restart_map())
def amqp_changed():
    if 'amqp' not in CONFIGS.complete_contexts():
        log('amqp relation incomplete. Peer not ready?')
        return
    CONFIGS.write_all()


@hooks.hook('neutron-control-relation-changed')
@restart_on_change(restart_map(), stopstart=True)
def restart_check():
    CONFIGS.write_all()


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    assess_status(CONFIGS)


if __name__ == '__main__':
    main()
