# Raspberry Pi 5: Three‑tier storage setup

This guide shows how to run CogniStore on a Raspberry Pi 5 with three POSIX tiers:
- Hot: 1 TB NVMe (fast local)
- Warm: 4 TB external SSD (USB 3)
- Cold: 24 TB external HDD (USB 3 or DAS)

We’ll mount each device, create directories for buckets, provide a `drivers.yaml`, and run a small workflow.

> Note: Commands assume Raspberry Pi OS/Debian. Adjust for your distro as needed. Run as a user in the `sudo` group.

## 1) Identify devices

```bash
lsblk -o NAME,MODEL,SIZE,FSTYPE,MOUNTPOINT
sudo blkid
```
Record the device paths and UUIDs, e.g.:
- NVMe: `/dev/nvme0n1p1` (UUID=XXXX-XXXX)
- SSD: `/dev/sda1` (UUID=YYYY-YYYY)
- HDD: `/dev/sdb1` (UUID=ZZZZ-ZZZZ)

## 2) Format (if needed)

Use ext4 for simplicity (or your preferred FS). WARNING: This erases data.
```bash
sudo mkfs.ext4 -F /dev/nvme0n1p1
sudo mkfs.ext4 -F /dev/sda1
sudo mkfs.ext4 -F /dev/sdb1
```

## 3) Create mount points

```bash
sudo mkdir -p /mnt/hot /mnt/warm /mnt/cold
```

## 4) Add to /etc/fstab

Get UUIDs from `blkid` and add entries (use `noatime` to reduce write amplification):
```
UUID=XXXX-XXXX  /mnt/hot   ext4  defaults,noatime  0  2
UUID=YYYY-YYYY  /mnt/warm  ext4  defaults,noatime  0  2
UUID=ZZZZ-ZZZZ  /mnt/cold  ext4  defaults,noatime  0  2
```
Apply mounts:
```bash
sudo mount -a
```

## 5) Prepare directories

```bash
sudo mkdir -p /mnt/hot/cognistore /mnt/warm/cognistore /mnt/cold/cognistore
sudo chown -R $USER:$USER /mnt/{hot,warm,cold}/cognistore
```

## 6) Create a 3‑tier drivers config

Save this as `drivers.rpi.yaml` in your repo (update paths if different):
```yaml
tiers:
  hot:
    driver: posix
    path: /mnt/hot/cognistore
  warm:
    driver: posix
    path: /mnt/warm/cognistore
  cold:
    driver: posix
    path: /mnt/cold/cognistore
```

## 7) Smoke test with CLI

Set up a virtualenv and install deps on the Pi, then:
```bash
# Put a file in hot
python -m cognistore.cli --drivers drivers.rpi.yaml put demo-bucket demo/hello.txt README.md

# List in hot
python -m cognistore.cli --drivers drivers.rpi.yaml ls-tier hot demo-bucket --prefix demo/

# Move from hot -> warm
python -m cognistore.cli --drivers drivers.rpi.yaml move hot warm demo-bucket demo/hello.txt

# Verify in warm
python -m cognistore.cli --drivers drivers.rpi.yaml ls-tier warm demo-bucket --prefix demo/
```

## 8) Build a catalog and run a policy pass

```bash
CAT_DB=/mnt/hot/cognistore/catalog.db

# Scan tiers (start with hot)
python -m cognistore.cli --drivers drivers.rpi.yaml --catalog-db "$CAT_DB" \
  catalog-scan hot demo-bucket --prefix demo/

# Run simple size-based policy across the bucket (e.g., <=1MiB hot, >1MiB warm)
python -m cognistore.cli --drivers drivers.rpi.yaml --catalog-db "$CAT_DB" \
  policy-run demo-bucket --threshold 1048576 --allowed-tiers hot,warm
```

## 9) Adding the cold tier to policies

You can extend policies to demote large/rare objects to `cold`:
- SimplePolicy currently supports two-way hot/warm. For three tiers, use ContentAwarePolicy with name/mime hints and size fallback, or an LLM-like provider with thresholds and tier preferences.
- Example content-aware demotion pattern:
```bash
python -m cognistore.cli --drivers drivers.rpi.yaml --catalog-db "$CAT_DB" \
  policy-run demo-bucket --policy content --allowed-tiers hot,warm,cold \
  --warm-name "*.zip" --warm-mime application/zip \
  --threshold 1048576
# For very large archives, you could run an additional pass that maps patterns to cold (future enhancement).
```

## 10) Performance tips on Raspberry Pi

- Use `noatime`, avoid frequent small writes.
- Prefer larger batch moves; keep concurrency modest.
- Monitor I/O: `iostat -xz 2`, `iotop`, `dstat`.
- Temperature and throttling: `vcgencmd measure_temp`.
- Consider enabling zram swap to avoid SD wear.

## 11) Troubleshooting

- Permissions: ensure the user running CogniStore owns the mount directories.
- Mounts: if `/mnt/*` are empty, re-check `/etc/fstab` and `sudo mount -a`.
- MIME metadata missing: run `catalog-scan` before content-aware policies.
- Python env: prefer a venv per project; avoid system Python.

---

You’re now set to experiment with a three-tier Pi setup. If you’d like, we can add cold-tier demotion rules and a dry-run preview to policies next.
