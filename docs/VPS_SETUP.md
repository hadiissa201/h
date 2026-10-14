# Running the collector on a free VPS

The local machine cannot do this job, and the reason is measured rather than
assumed. Over 43.9 elapsed hours it was connected for 4.85: **11% uptime**,
with **21 of 24 hours of the day under 50% coverage** and the best hour at
50%. The cause is intermittent mains power, which no reconnect logic reaches.

That is not a smaller sample. Liquidations follow the trading sessions, so
being blind in the same hours every day samples a different thing, and no
figure computed from the returns would reveal it. The plan needs 20 distinct
days; at this coverage the result would be uninterpretable however long it
ran.

A free cloud VM fixes, in one move, every obstacle hit so far: mains power,
DNS resolution failures, the IPv6 route that was timing out, the laptop
sleeping, and Binance futures being region-blocked.

## Oracle Cloud Always Free

Permanently free, not a trial. A card is required for identity verification
and is not charged while you stay inside the free allowances.

**Pick the region carefully.** Bybit restricts access from the United States,
so choose Frankfurt, Amsterdam, Singapore or Tokyo. The region cannot be
changed after signup, so getting this wrong means starting again.

### 1. Create the instance

Console → **Compute** → **Instances** → **Create instance**.

- Image: **Ubuntu 22.04** or 24.04
- Shape: **VM.Standard.A1.Flex**, 1 OCPU and 6 GB is ample
- Add your SSH public key (generate one locally with `ssh-keygen -t ed25519`)

If you get **"Out of host capacity"**, that is Oracle's ARM pool being full in
that region and it is common. Either retry over a few hours, or use
**VM.Standard.E2.1.Micro** instead, which is also Always Free, weaker, and far
more available. The collector needs almost nothing, so the micro is fine.

### 2. Connect and set up

```bash
ssh ubuntu@<public-ip>

sudo apt update && sudo apt install -y python3-pip python3-venv git tmux
git clone https://github.com/hadiissa201/h.git
cd h
git checkout claude/ai-crypto-trading-system-xvz20l

python3 -m venv .venv
.venv/bin/pip install websockets httpx
```

### 3. Check it can reach the venue before committing to a long run

```bash
.venv/bin/python scripts/liquidations/collect.py --hours 0.1
```

Within seconds this prints either `bybit futures: reachable` or the reason it
is not. If the region is blocked it says so immediately rather than looking
like a quiet market — that check exists because eight hours were once lost to
exactly that confusion.

### 4. Run it so it survives everything

A 20-day run should not depend on an SSH session staying open. systemd
restarts it after a crash, a network drop, or a reboot.

```bash
sudo tee /etc/systemd/system/liquidations.service > /dev/null <<'EOF'
[Unit]
Description=Liquidation cascade collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/h
ExecStart=/home/ubuntu/h/.venv/bin/python scripts/liquidations/collect.py
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/collector.log
StandardError=append:/home/ubuntu/collector.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now liquidations
```

Check on it:

```bash
systemctl status liquidations
tail -f ~/collector.log
```

### 5. Read the data

From the VM:

```bash
cd ~/h && .venv/bin/python scripts/liquidations/rate.py
```

Or copy the database to your own machine and run it there:

```powershell
scp ubuntu@<public-ip>:~/h/data/liquidations.sqlite .
```

## Oracle stops instances it considers idle

Oracle reclaims Always Free compute it judges unused. Per [Oracle's
docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm),
an instance is idle when, over a 7-day window, 95th-percentile CPU is under
20%, network is under 20%, and (on A1 shapes) memory is under 20%.

**This collector trips all three.** It holds one websocket and waits. CPU is
near zero, the traffic is a few KB/s, and it uses a few dozen MB of RAM. Left
alone it would be stopped around day 7 of a 20-day run -- and it would look
exactly like the power cuts it was meant to escape.

The instance is stopped, not deleted, and the disk survives, so restarting it
loses no data. But the gap in collection is the thing that makes the sample
unusable, so do not rely on noticing.

`keepalive.py` holds CPU above the threshold with a duty-cycled loop. The
instance genuinely is in continuous use; it is I/O-bound rather than
CPU-bound, so this makes the utilisation metric reflect what is actually
happening rather than disguise an unused machine.

```bash
sudo tee /etc/systemd/system/keepalive.service > /dev/null <<'EOF'
[Unit]
Description=Hold CPU above the Always Free idle threshold

[Service]
Type=simple
User=ubuntu
ExecStart=/home/ubuntu/h/.venv/bin/python /home/ubuntu/h/scripts/liquidations/keepalive.py
Restart=always
Nice=19

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now keepalive
```

`Nice=19` means it yields the CPU to the collector whenever the collector
wants it, so it cannot cause a missed liquidation.

If you would rather not run it: check the instance every few days and restart
it when Oracle stops it. Oracle emails first. Converting to Pay As You Go also
exempts the account, but that makes overages billable, so it is the wrong
trade here.

## What to confirm after a day

Run `rate.py` and check three things, in this order:

1. **Uptime above 90%.** If not, the VM is not the fix and something else is
   wrong.
2. **No hour under 50% coverage.** Flat coverage is what makes the sample
   interpretable; this is the number the local machine could not reach.
3. **Cascades accumulating at the pre-registered $250k.** The local data put
   this at 19.8/day while connected, so 20 days should clear the 100 the plan
   requires -- with the clustering caveat that 100 cascades is roughly 50
   independent episodes.

## What a VPS does not fix

- **Detection latency.** Measured at 1.86s median during real bursts against
  a 2s precondition. A data centre is closer to the venue than a home
  connection, so this should improve, but it is measured on every cascade and
  the plan fails on it if it does not.
- **Whether the effect exists.** The point of all of this is to find out, and
  the answer may well be no.
