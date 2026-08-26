#!/bin/sh
# Copy the read-only mounted config into FRR's own dir, then start the
# daemons (per /etc/frr/daemons) and keep the container alive.
set -e
cp /config/daemons /etc/frr/daemons
cp /config/frr.conf /etc/frr/frr.conf
chown -R frr:frr /etc/frr
chmod 640 /etc/frr/daemons /etc/frr/frr.conf
/usr/lib/frr/frrinit.sh start
exec sleep infinity
