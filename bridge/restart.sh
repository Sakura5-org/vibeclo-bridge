#!/bin/bash
# Safe CLO 2 bridge restart: it takes no new turns, exits once running ones finish, launchd/systemd starts the new code.
label=com.clo2.bridge
if [[ "$(uname -s)" == Linux ]]; then
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  pid=$(systemctl --user show -p MainPID --value clo2-bridge 2>/dev/null)
else
  pid=$(launchctl list | awk -v l=$label '$3==l {print $1}')
fi
if [[ -n $pid && $pid != 0 && "$(cat ~/.clo2/graceful 2>/dev/null)" == "$pid" ]]; then
  [[ -e ~/.clo2/restart ]] || touch ~/.clo2/restart
  echo "CLO 2 bridge restart requested (after running turns finish)"
elif [[ "$(uname -s)" == Linux ]]; then
  systemctl --user restart clo2-bridge && echo "CLO 2 bridge restarted"
else
  launchctl kickstart -k gui/$(id -u)/$label && echo "CLO 2 bridge restarted"
fi
