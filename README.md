# Wikidata PostgreSQL Server Setup

This README documents the system-level setup of the Wikidata PostgreSQL
server on the ThinkPad Edge E540, including attaching the two 1 TB
drives, combining them with LVM, mounting the resulting filesystem, and
creating a PostgreSQL 14 cluster directly on the LVM volume.

> **Warning:** `pvcreate`, `vgcreate`, `lvcreate`, `mkfs`, and
> partitioning commands can destroy existing data. Always identify disks
> with `lsblk` before running them. The device names below (`/dev/sdb`,
> `/dev/sdc`) describe this specific machine and must not be assumed to
> be stable after hardware changes.

## Resulting layout

The machine currently uses:

``` text
/dev/sda                         System SSD (do not modify)
├── Ubuntu
└── Windows

/dev/sdb                         1 TB WDC WD10JPVX-08JC3T5
└── /dev/sdb1                    LVM physical volume
    └── wiki_vg/wiki_lv

/dev/sdc                         1 TB ST1000LM010-9YH146
└── /dev/sdc1                    LVM physical volume
    └── wiki_vg/wiki_lv

wiki_vg/wiki_lv                  ~1.82 TiB ext4
└── /data/wiki
    └── postgresql/14/main       PostgreSQL data directory
```

This is an **LVM linear volume**, not RAID. Capacity from both drives is
combined into one filesystem. It provides no redundancy: failure of
either physical drive can make the complete logical volume unusable.

## 1. Connect and identify the drives

Connect both drives and verify how Linux detected them:

``` bash
lsblk -o NAME,SIZE,MODEL,FSTYPE,MOUNTPOINTS
```

On this machine the relevant drives were:

``` text
sdb  931.5G  WDC WD10JPVX-08JC3T5
sdc  931.5G  ST1000LM010-9YH146
```

The internal system SSD is `/dev/sda` and must not be included in the
LVM setup.

Before creating LVM volumes, inspect the disks carefully:

``` bash
sudo fdisk -l
lsblk -f
```

If the target drives contain data that must be retained, stop here and
back it up first.

## 2. Prepare partitions for LVM

Each storage drive needs a partition assigned to LVM. In the resulting
setup these are:

``` text
/dev/sdb1
/dev/sdc1
```

The exact partitioning procedure depends on the previous state of the
disks. Do not blindly recreate partitions on an already configured
server.

After partitioning, verify:

``` bash
lsblk -o NAME,SIZE,MODEL,FSTYPE
```

## 3. Create LVM physical volumes

**Destructive when applied to the wrong partition. Verify `/dev/sdb1`
and `/dev/sdc1` first.**

``` bash
sudo pvcreate /dev/sdb1 /dev/sdc1
```

Verify:

``` bash
sudo pvs
```

## 4. Create the volume group

Create a volume group named `wiki_vg`:

``` bash
sudo vgcreate wiki_vg /dev/sdb1 /dev/sdc1
```

Verify:

``` bash
sudo vgs
```

The resulting volume group is approximately 1.82 TiB.

A warning about inconsistent physical block sizes may appear because the
two drives expose different physical sector sizes. This configuration
was intentionally created despite that warning.

## 5. Create the logical volume

Allocate all available space to one logical volume:

``` bash
sudo lvcreate -l 100%FREE -n wiki_lv wiki_vg
```

Verify:

``` bash
sudo lvs
```

The logical device is available as:

``` text
/dev/wiki_vg/wiki_lv
```

and through the device-mapper path:

``` text
/dev/mapper/wiki_vg-wiki_lv
```

## 6. Create the ext4 filesystem

**This destroys existing filesystem contents on the logical volume. Run
only when creating the volume for the first time.**

``` bash
sudo mkfs.ext4 /dev/wiki_vg/wiki_lv
```

The automatically created `lost+found` directory is normal for ext4. It
is reserved for filesystem recovery by `fsck`.

## 7. Mount at `/data/wiki`

Create the mount point:

``` bash
sudo mkdir -p /data/wiki
```

Get the filesystem UUID:

``` bash
sudo blkid /dev/wiki_vg/wiki_lv
```

Add the filesystem to `/etc/fstab` using its UUID rather than relying on
device names:

``` text
UUID=<UUID-FROM-BLKID>  /data/wiki  ext4  defaults  0  2
```

Edit the file with:

``` bash
sudo nano /etc/fstab
```

Then test the configuration without rebooting:

``` bash
sudo mount -a
```

Verify:

``` bash
findmnt /data/wiki
df -h /data/wiki
```

The current filesystem is approximately:

``` text
Filesystem                    Size  Mounted on
/dev/mapper/wiki_vg-wiki_lv   1.8T  /data/wiki
```

## 8. Verify LVM after reconnecting or rebooting

The normal health/attachment check is:

``` bash
lsblk -o NAME,SIZE,MODEL,FSTYPE,MOUNTPOINTS
sudo pvs
sudo vgs
sudo lvs
df -h /data/wiki
```

Expected LVM structure:

``` text
PV          VG       Size       Free
/dev/sdb1   wiki_vg  ~931.51G   0
/dev/sdc1   wiki_vg  ~931.51G   0

VG       #PV  #LV  Size
wiki_vg    2    1  ~1.82T

LV       VG       Size
wiki_lv  wiki_vg  ~1.82T
```

Check kernel logs for storage errors:

``` bash
sudo dmesg | grep -Ei 'sdb|sdc|I/O error|blk_update|Buffer I/O|EXT4-fs error'
```

## 9. Check drive SMART data

Install SMART tools if necessary:

``` bash
sudo apt install smartmontools
```

Then inspect each drive:

``` bash
sudo smartctl -a /dev/sdb
sudo smartctl -a /dev/sdc
```

Some USB-to-SATA bridges do not expose SMART automatically. If
`smartctl` reports an unknown USB bridge, determine the appropriate `-d`
device type before attempting further SMART operations.

For HDD health, particularly inspect:

``` text
Reallocated_Sector_Ct
Current_Pending_Sector
Offline_Uncorrectable
UDMA_CRC_Error_Count
SMART overall-health
SMART Error Log
```

## 10. Install PostgreSQL

Update package metadata:

``` bash
sudo apt update
```

Install PostgreSQL and contributed extensions:

``` bash
sudo apt install postgresql postgresql-contrib
```

Verify:

``` bash
psql --version
sudo systemctl status postgresql --no-pager
pg_lsclusters
```

This server currently uses PostgreSQL 14.

## 11. Create PostgreSQL directly on the LVM volume

The PostgreSQL packages were installed before a database cluster
existed, so the cluster could be initialized directly on `/data/wiki`.

Create its data directory:

``` bash
sudo mkdir -p /data/wiki/postgresql/14/main
sudo chown -R postgres:postgres /data/wiki/postgresql
sudo chmod 700 /data/wiki/postgresql/14/main
```

Create and start the PostgreSQL cluster:

``` bash
sudo pg_createcluster 14 main \
  --datadir=/data/wiki/postgresql/14/main \
  --start
```

Verify:

``` bash
pg_lsclusters
```

Expected:

``` text
Ver Cluster Port Status Owner    Data directory
14  main    5432 online postgres /data/wiki/postgresql/14/main
```

Test PostgreSQL itself:

``` bash
cd /tmp
sudo -u postgres psql -c "SELECT version();"
```

Using `/tmp` avoids the harmless warning that occurs when the `postgres`
user cannot enter the current user's home directory.

## 12. Boot dependency: PostgreSQL must not start without `/data/wiki`

Because the PostgreSQL data directory resides on the LVM filesystem,
`/data/wiki` must be mounted before PostgreSQL attempts to start.

After configuring `/etc/fstab`, verify after a reboot:

``` bash
findmnt /data/wiki
pg_lsclusters
```

Do not run PostgreSQL against an accidentally unmounted `/data/wiki`
directory. If the storage is unavailable, diagnose and restore the mount
first.

## 13. Database-specific setup

System/storage setup ends here.

The following operations should be managed by the Wikidata pipeline
rather than repeated manually:

``` text
- creating the wikidata database
- installing/enabling PostGIS
- creating tables and indexes
- importing Wikidata
- migrating existing extracted data
- validating imported data
- resumable/checkpointed dump processing
```

PostgreSQL's cluster is already physically located at:

``` text
/data/wiki/postgresql/14/main
```

Therefore databases created inside this cluster automatically reside on
the LVM filesystem; application scripts should not move PostgreSQL's
physical files themselves.

### Pipeline template

The database-specific template lives in [`wikidata_pipeline/`](wikidata_pipeline/).
Run it **on the server**, after the cluster is online. It creates the
`wikidata` database, enables PostGIS, and stores Wikidata entities as JSONB
and Wikipedia pages as XML revision text. It never moves PostgreSQL files.

Install the PostgreSQL 14 PostGIS package on the server if it is not already
available (for example, `postgresql-14-postgis-3` on Ubuntu), then install
the Python dependency in a virtual environment:

``` bash
cd wikidata_pipeline
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Connect as a role that can create a database and extensions for `setup`.
The default libpq connection strings are `dbname=postgres` for the admin
connection and `dbname=wikidata` for the target. Set `WIKIDATA_ADMIN_DSN`
and `WIKIDATA_DSN` to override them. Ordinary libpq environment variables
such as `PGHOST`, `PGUSER`, and `PGPASSWORD` also work. The target DSN must
point to the `wikidata` database created by setup.

``` bash
.venv/bin/python main.py import-all
.venv/bin/python main.py validate
```

Filesystem paths are collected in [`wikidata_pipeline/const.py`](wikidata_pipeline/const.py).
`main.py import-all` reads `/home/ilia/Data/wiki/latest-all.json.bz2` and
`/home/ilia/Data/wiki/enwiki-latest-pages-articles-multistream.xml.bz2` by
default. Use `--entries-path` and `--pages-path` to override them. The
PostgreSQL path in `const.py` remains the location documented in the setup
steps above. Changing it does not move or reconfigure a cluster; check the
actual location with `pg_lsclusters` on the server.

`main.py` calls `wikidata_entries.py` and `wikipedia_pages.py`. Both use
`helper.py` for database setup, checkpoints, batch transactions, and graceful
pause. To import one source, use `main.py import-entries` or `main.py
import-pages`, optionally followed by a different file path. The old
`pipeline.py import-dump` command still works for Wikidata entries.

The entries importer accepts plain JSON, `.gz`, or `.bz2`. It reads a Wikidata
line-oriented JSON array one entity at a time and commits 1,000 entities
per batch by default. It never expands the whole dump on disk or in memory.
The pages importer streams the compressed XML and stores page ID, title,
namespace, redirect title, revision ID, and raw wikitext in
`wikipedia_pages`. It imports every page in the archive; it does not render
wikitext or link pages to Wikidata entities.

Each importer commits a checkpoint with each batch. Rerunning the same
unchanged file resumes after committed records, although it must reread the
compressed prefix to reach the checkpoint. A changed file starts a new
checkpoint. Upserts make reruns safe. Press `Ctrl+C` to commit the current
partial batch and pause; run the same command to resume. `validate` reports
both table counts and saved positions. For the full dumps, plan for database
space beyond the compressed file sizes and a long import time.

Migration is available when an existing PostgreSQL table has a unique,
non-null text ID and a JSON or JSONB column containing each complete
entity document. Keep the source table stable while migrating. Set the
source connection and table/column names for that server:

``` bash
export WIKIDATA_SOURCE_DSN='host=localhost dbname=existing_db user=your_user'
.venv/bin/python pipeline.py migrate --source-table public.entities \
  --id-column id --json-column data
.venv/bin/python pipeline.py validate
```

Migration commits batches with the last copied ID so it can resume after
an interruption. It upserts by entity ID. The source table name and column
names are quoted as SQL identifiers. If the source uses another structure,
adapt `migrate()` to extract the desired entity JSON before running it.
`validate` reports row counts and recorded checkpoints; it does not prove
that every source entity was imported. Compare its count with the source
count, and inspect representative rows before relying on the data.

## Useful status commands

``` bash
# Physical/block-device layout
lsblk -o NAME,SIZE,MODEL,FSTYPE,MOUNTPOINTS

# LVM
sudo pvs
sudo vgs
sudo lvs

# Mounted filesystem
findmnt /data/wiki
df -h /data/wiki

# PostgreSQL
pg_lsclusters
sudo systemctl status postgresql --no-pager

# PostgreSQL log
sudo tail -100 /var/log/postgresql/postgresql-14-main.log

# Relevant kernel storage errors
sudo dmesg | grep -Ei 'sdb|sdc|I/O error|blk_update|Buffer I/O|EXT4-fs error'
```

## Important recovery rule

Do **not** respond to a missing drive or missing LVM volume by running
any of these commands again:

``` text
pvcreate
vgcreate
lvcreate
mkfs.ext4
```

Those are provisioning commands, not reconnection/recovery commands.

If the volume disappears after a reboot or reconnect, first inspect:

``` bash
lsblk
sudo pvs
sudo vgs
sudo lvs
sudo vgchange -ay wiki_vg
findmnt /data/wiki
```

and diagnose the existing LVM metadata before making changes.
