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
- creating tables and indexes
- importing Wikidata
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
`wikidata` database and stores Wikidata entities as JSONB
and Wikipedia pages as XML revision text. It never moves PostgreSQL files.

Install the Python dependency in a virtual environment:

``` bash
cd wikidata_pipeline
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Connect as a role that can create a database during setup.
The default libpq connection strings are `dbname=postgres` for the admin
connection and `dbname=wikidata` for the target. Both are defined in
`Settings` in `wikidata_pipeline/const.py`. Ordinary libpq environment variables
such as `PGHOST`, `PGUSER`, and `PGPASSWORD` also work. The target DSN must
point to the `wikidata` database created by setup.

``` bash
.venv/bin/python main.py --build-wikidata-index
.venv/bin/python main.py --import-all
```

Filesystem paths are collected in [`wikidata_pipeline/const.py`](wikidata_pipeline/const.py).
`main.py` reads `/home/ilia/Data/wiki/latest-all.json.bz2` and
`/home/ilia/Data/wiki/enwiki-latest-pages-articles-multistream.xml.bz2`.
The Wikipedia import also requires the matching
`enwiki-latest-pages-articles-multistream-index.txt.bz2` in the same
directory. Edit `Settings` in `const.py` to change the file locations. The PostgreSQL path
in `Settings` remains the location documented in the setup
steps above. Changing it does not move or reconfigure a cluster; check the
actual location with `pg_lsclusters` on the server.

### Why the pipeline has separate scripts

The dumps have different formats and different ways to resume. Keeping their
readers separate lets each use the right index while sharing the database and
checkpoint behavior:

| File | Responsibility and reason |
| --- | --- |
| `main.py` | `PipelineCLI` provides two explicit actions. `--build-wikidata-index` prepares only the Wikidata dump; `--import-all` sets up PostgreSQL, imports entries and then pages, and reports counts. Index creation is separate because it requires a full scan and may take hours. |
| `wikidata_index.py` | `WikidataBlockIndex` builds and loads the Wikidata bzip2 block map. The JSON dump has no companion multistream index, so this map must be generated locally. |
| `wikidata_entries.py` | `WikidataEntryImporter` reads the Wikidata JSON array one entity at a time and stores each complete entity as JSONB. Its checkpoint includes the decoded byte position needed for a fast indexed restart. |
| `wikipedia_pages.py` | `WikipediaPageImporter` reads the Wikipedia XML using its matching multistream index and stores page fields and raw wikitext. Its compressed streams can be opened at known byte positions. |
| `helper.py` and `schema.sql` | `Database`, `WorkerPlanner`, `OrderedBatchWriter`, `ProgressBar`, and `StopSignal` share setup, worker sizing, ordered writes, progress, and stop handling. The schema stores checkpoints. Rows and their checkpoint are committed in the same transaction, so a restart does not skip an unwritten batch. |
| `const.py` | `Settings` holds dump paths, index paths, PostgreSQL connection strings, and fixed settings in one place. |

### Why the indexes matter

`--build-wikidata-index` scans `latest-all.json.bz2` with parallel bzip2
decompression and saves block offsets as `latest-all.json.bz2.blocks.json`
beside the dump. It reads the compressed file once without writing an
uncompressed copy. A valid existing index skips this scan. The index belongs
to the exact dump version; rebuild it after replacing the dump. The command
shows a progress bar based on compressed bytes scanned.

The entry importer saves a decompressed byte offset with each committed
batch. On restart, the block map lets it seek near that offset and continue
with the next entity. If the map is absent, the importer warns and asks
whether to continue. Continuing still saves byte offsets, but every restart
must decompress and scan from the beginning to skip committed entries. An
older checkpoint without an offset needs one such scan even after the index
is built.

Wikipedia provides a separate
[multistream index](https://meta.wikimedia.org/wiki/Data_dumps/Dump_format#Multistream_dumps)
with compressed stream positions. The page importer uses it to restart at
the stream containing the next page, then skips only pages already committed
within that stream. If this companion index is missing, the script downloads
it after checking that the local XML dump has the current
[Wikimedia enwiki latest dump](https://dumps.wikimedia.org/enwiki/latest/)
size. An index from another run may have different offsets; if the local XML
differs from `latest`, provide its matching index yourself.

### Why imports use batches and checkpoints

The dumps are too large to expand into memory, and a full uncompressed copy
would consume substantial disk space. The importers read them in small parts
instead. The entry importer accepts plain JSON, `.gz`, or `.bz2`, parses
one entity at a time, and stores it in `entities` as JSONB. The page importer
stores page ID, title, namespace, redirect title, revision ID, and raw
wikitext in `wikipedia_pages`; it does not render wikitext or link pages to
Wikidata entities. Both commit up to 1,000 rows per batch by default; an
8 MiB byte limit can make a batch smaller.

`--import-all` uses the parallel decoder and bounded worker queues adapted
from the preprocessing approach in the separate tripplanner project. It
still stores every Wikidata entity and every Wikipedia page; it does not use
that project's POI or country filters. The script chooses its worker counts
on the machine where it runs, using available CPU and memory. This E540 has
an Intel Core i5-4200M (two cores, four threads) and 16 GiB of RAM. With at
least 4 GiB currently available, it uses two bzip2 workers, one JSON parser
process, and one PostgreSQL writer thread.
With less available memory or fewer CPUs, it reduces the bzip2 worker count
to one. It allows a second parser only with at least eight CPUs and 8 GiB
available memory. The single writer overlaps SQL with reading and parsing,
while keeping batch checkpoints in source order. Queues and batches are
bounded by record count and bytes to limit memory use alongside PostgreSQL.
Wikipedia multistreams are decoded sequentially, with SQL writing in the
background; this retains the stream order needed for page checkpoints. The
data volume is on two external USB disks, so one ordered SQL writer also
avoids adding several competing write streams to that storage.

Each batch and its checkpoint are committed together. A finished source is
marked complete and skipped on later runs; a changed dump gets a new source
identity and a new checkpoint. Upserts make repeated rows safe. Press
`Ctrl+C` to commit the current partial batch and pause, then run the same
command to resume. `--import-all` runs entries before pages and reports table
counts and saved positions when both finish. To run one importer directly,
use `.venv/bin/python wikidata_entries.py` or
`.venv/bin/python wikipedia_pages.py` after setting up the database. For the
full dumps, plan for database space beyond their compressed sizes and a long
import time. Compare the reported counts with the source dumps and inspect
representative rows before relying on the data.

During `--import-all`, separate progress bars show compressed-file progress
and the number of entries or pages read. The percentage estimates how far
the reader has moved through each compressed dump; committed progress is
stored in PostgreSQL after each batch. A resumed import can jump forward
when it reaches its saved position.

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
