# azure-region-speedtest

Benchmark blob transfer throughput between Azure regions using AzCopy.

The script creates a storage account + container in each of a list of Azure
regions, generates a random test file (5GB by default), and times AzCopy
transfers:

1. **local -> region** — upload from the machine running the script to each region
2. **region -> region** — transfer between every pair of regions
3. **intra-region** — transfer within the same region, as a baseline

Results are written to `performance_data.csv` (columns: `from`, `to`,
`Total Time (s)`, `MB/sec`), with a detailed run log in `performance_log.txt`.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), logged
  in (`az login`) with permission to create storage accounts and (optionally)
  role assignments in the target subscription/resource group.
- An existing Azure resource group to create the storage accounts in.
- [AzCopy](https://learn.microsoft.com/azure/storage/common/storage-use-azcopy-v10)
  installed and on your `PATH` (or point `--azcopy-path` / `AZCOPY_PATH` at it).
  AzCopy needs to be authenticated — either run `azcopy login` yourself first,
  or run this on an Azure VM with a managed identity (the script calls
  `azcopy login --identity`).
- Python 3.8+ and the packages in `requirements.txt`.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python speedtest.py \
  --subscription-id <your-subscription-id> \
  --resource-group <your-resource-group> \
  --storage-prefix <a-globally-unique-prefix>
```

Run `python speedtest.py --help` for all options, including:

- `--regions` — override the default list of Azure regions to test
- `--file-size-gb` — change the test file size (default 5)
- `--assignee-object-id` — grant an extra identity `Storage Blob Data
  Contributor` access on each storage account (skip if your own identity
  already has access)
- `--skip-create` — reuse storage accounts from a previous run instead of
  recreating them

All of the above (except `--regions`/`--file-size-gb`) can also be set via
environment variables: `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
`STORAGE_ACCOUNT_PREFIX`, `AZURE_ASSIGNEE_OBJECT_ID`, `AZCOPY_PATH`.

## Cleanup

This script creates real, billable storage accounts and does not delete them.
When you're done, remove them, e.g.:

```bash
az group delete --name <your-resource-group> --yes --no-wait
```

(Only delete the resource group if it doesn't contain other resources you
want to keep.)
