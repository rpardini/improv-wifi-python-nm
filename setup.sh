#!/usr/bin/env bash

set -e
set -x

apt update
apt install python3-pip
pip3 install bluez-peripheral

systemctl stop improv-wifi.service || true

cp improv-wifi.service /usr/lib/systemd/system/improv-wifi.service
systemctl daemon-reload

systemctl enable improv-wifi.service
systemctl start improv-wifi.service

echo "Done"

