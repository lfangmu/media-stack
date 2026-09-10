#!/bin/bash
# Route B (NAS side): restore foreign-egress NAT for OpenClash VM 192.168.68.2
#
# Why: the upstream gateway (192.168.68.1) allows THIS nas (192.168.68.68) to reach the
#      internet but DROPS the OpenClash VM (192.168.68.2) by source IP. So the VM's traffic
#      must be NAT-ed through this host, otherwise every Clash node times out, GLOBAL
#      falls back to DIRECT and egress dies completely.
#
# Idempotent: each rule is checked (-C) before insert, safe to run repeatedly.
# Called by root crontab @reboot and by install.sh.
#
# IMPORTANT: this file lives in the media-stack repo on purpose. A fresh clone/deploy
# wipes /opt/media, and when this script is missing the NAT rules never come back
# after a reboot -> total egress outage (happened 2026-09-10).
set -u

VMIP="${VM_IP:-192.168.68.2}"
# 自动探测默认出网网卡（可用 EGRESS_OUT_IF 覆盖）
OUT_IF="${EGRESS_OUT_IF:-$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')}"

if [ -z "$OUT_IF" ]; then
  echo "$(date '+%F %T') restore_vm_egress: FATAL cannot detect outbound iface" >&2
  exit 1
fi

echo 1 > /proc/sys/net/ipv4/ip_forward

iptables -t nat -C POSTROUTING -s "$VMIP" -o "$OUT_IF" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s "$VMIP" -o "$OUT_IF" -j MASQUERADE

iptables -C FORWARD -s "$VMIP" -o "$OUT_IF" -j ACCEPT 2>/dev/null \
  || iptables -I FORWARD -s "$VMIP" -o "$OUT_IF" -j ACCEPT

iptables -C FORWARD -d "$VMIP" -i "$OUT_IF" -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \
  || iptables -I FORWARD -d "$VMIP" -i "$OUT_IF" -m state --state ESTABLISHED,RELATED -j ACCEPT

echo "$(date '+%F %T') restore_vm_egress: iface=$OUT_IF vm=$VMIP ip_forward=$(cat /proc/sys/net/ipv4/ip_forward) rules_ok"
