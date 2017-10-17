#!/bin/sh

### BEGIN INIT INFO
# Provides:          networking-sriov
# Required-Start:    mountkernfs $local_fs
# Required-Stop:     $local_fs
# Default-Start:     S
# Default-Stop:      0 6
# Short-Description: Configure SRIOV Virtual Functions
### END INIT INFO

# Authors: Frode Nordahl <frode.nordahl@gmail.com>

DESC="Configure SRIOV Virtual Functions"

. /lib/lsb/init-functions

# Include defaults if available
if [ -f /etc/default/neutron-openvswitch-networking-sriov ] ; then
    . /etc/default/neutron-openvswitch-networking-sriov
fi

if [ -z "${ENABLE}" ]; then
    ENABLE=0
fi

if [ -z "${VFS_BLANKET}" ]; then
    VFS_BLANKET=auto
fi

# Exit if feature is not enabled
[ $ENABLE -gt 0 ] || exit 0

do_wait_for_vfs() {
    # Wait for VFs to be created
    PCI_DEVICE_PATH=$1
    NUMVFS=$2
    [ -z "${DEBUG}" ] || echo "do_wait_for_vfs ${PCI_DEVICE_PATH} ${NUMVFS}"
    while [ `ls -1 "${PCI_DEVICE_PATH}/" | wc -l` -lt "${NUMVFS}" ]; do
        [ -z "${DEBUG}" ] || echo wait...
        sleep 0.05
    done
}

do_configure_vfs() {
    if [ ! -z "${VFS_LIST}" ]; then
        # Do configuration according to list of <device>:<numvfs> tuples
        OIFS="$IFS"
        echo "${VFS_LIST}" | \
            while IFS=':' read DEVICE NUMVFS; do
                # Check whether we should stop
                [ -z "${STOP}" ] || NUMVFS=0
                [ -z "${DEBUG}" ] || echo "echo ${NUMVFS} \> /sys/class/net/${DEVICE}/device/sriov_numvfs"
                echo "${NUMVFS}" > "/sys/class/net/${DEVICE}/device/sriov_numvfs"
                do_wait_for_vfs "/sys/class/net/${DEVICE}/device" "${NUMVFS}"
            done
        IFS="$OIFS"
    else
        # Do blanket configuration
        SYSFS_LST=`find /sys/devices -name sriov_totalvfs`
        for ENT in $SYSFS_LST; do
            DENT=`dirname $ENT`
            if [ -d ${DENT}/net ]; then
                TOTALVFS=`cat "${ENT}"`
                [ -z "${STOP}" ] || VFS_BLANKET=0
                if [ "${VFS_BLANKET}" = "auto" ]; then
                    # Set sriov_numvfs to value of sriov_toatlvfs for "auto"
                    NUMVFS=$TOTALVFS
                elif [ "${VFS_BLANKET}" -gt "${TOTALVFS}" ]; then
                    # Set sriov_numvfs to value of sriov_totalvfs if
                    # requested number is larger than sriov_totalvfs
                        NUMVFS=$TOTALVFS
                else
                        NUMVFS=$VFS_BLANKET
                fi
                # Set sriov_numvfs to requested number
                [ -z "${DEBUG}" ] || echo "echo ${NUMVFS} \> ${DENT}/sriov_numvfs"
                echo "${NUMVFS}" > "${DENT}/sriov_numvfs"
                do_wait_for_vfs "${DENT}" "${NUMVFS}"
            fi
        done
    fi
}

do_start() {
    do_configure_vfs
}

do_stop() {
    STOP=1
    do_configure_vfs
}


case "$1" in
  start)
      log_daemon_msg "$DESC"
      do_start
      ;;
  stop)
      log_daemon_msg "Un-$DESC"
      do_stop
      ;;
  systemd-start)
      do_start
      ;;
  systemd-stop)
      do_stop
      ;;
  restart)
      log_daemon_msg "Re-$DESC"
      do_stop
      do_start
      ;;
  *)
      N=/etc/init.d/neutron-openvswitch-networking-sriov.sh
      echo "Usage: $N {start|stop|restart|systemd-start|systemd-stop}" >&2
      ;;
esac

exit 0
