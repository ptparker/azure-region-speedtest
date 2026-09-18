#!/usr/bin/env python3
"""
Azure cross-region file transfer speed test.

Spins up a storage account + blob container in each of a set of Azure
regions, generates a random test file (5GB by default), then uses AzCopy
to measure transfer throughput:
  1. from the local machine up to each region ("local" -> region)
  2. between every pair of regions (region -> region)
  3. within the same region (intra-region baseline)

Results are written to a CSV and a log file for later analysis.

Prerequisites:
  - Azure CLI (`az`) installed and logged in (`az login`) with permission to
    create storage accounts and role assignments in the target subscription
    and resource group.
  - AzCopy installed. On the VM/host running this script, either:
      * put azcopy on PATH, or
      * point --azcopy-path at the binary, or
      * set the AZCOPY_PATH environment variable.
    AzCopy must be authenticated (e.g. `azcopy login` or `azcopy login --identity`
    if running on an Azure VM with a managed identity) before running this script.
  - Python packages in requirements.txt (pandas).

This script creates real, billable Azure resources (storage accounts) and
does not delete them when finished. Remember to clean up afterwards, e.g.:
  az group delete --name <resource-group> --yes --no-wait

IMPORTANT: The subscription ID, resource group, storage account prefix, and
principal (object) ID to grant blob access to are all specific to your own
Azure environment. Pass them in via command-line arguments or environment
variables (see --help) rather than editing this file.
"""

import argparse
import logging
import os
import subprocess
import time

import pandas as pd

# Default set of Azure regions to test. Override with --regions if needed.
DEFAULT_REGIONS = [
    "eastus",
    "eastus2",
    "westus",
    "westus2",
    "francecentral",
    "swedencentral",
    "polandcentral",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Benchmark Azure cross-region blob transfer speed with AzCopy."
    )
    parser.add_argument(
        "--subscription-id",
        default=os.environ.get("AZURE_SUBSCRIPTION_ID"),
        required=os.environ.get("AZURE_SUBSCRIPTION_ID") is None,
        help="Azure subscription ID to create resources in. "
        "Can also be set via AZURE_SUBSCRIPTION_ID.",
    )
    parser.add_argument(
        "--resource-group",
        default=os.environ.get("AZURE_RESOURCE_GROUP", "speedtest-rg"),
        help="Resource group the storage accounts are created in "
        "(must already exist). Default: speedtest-rg, or AZURE_RESOURCE_GROUP.",
    )
    parser.add_argument(
        "--storage-prefix",
        default=os.environ.get("STORAGE_ACCOUNT_PREFIX", "speedtest"),
        help="Prefix used to build each region's storage account name "
        "(account name = <prefix><region>, must be globally unique, "
        "lowercase letters/numbers only, <=24 chars total).",
    )
    parser.add_argument(
        "--assignee-object-id",
        default=os.environ.get("AZURE_ASSIGNEE_OBJECT_ID"),
        help="Object ID of the user/group/managed identity to grant the "
        "'Storage Blob Data Contributor' role on each storage account. "
        "If omitted, role assignment is skipped (useful if your own "
        "identity already has access, e.g. via subscription-level RBAC).",
    )
    parser.add_argument(
        "--regions",
        nargs="+",
        default=DEFAULT_REGIONS,
        help=f"Azure regions to test. Default: {DEFAULT_REGIONS}",
    )
    parser.add_argument(
        "--file-size-gb",
        type=int,
        default=5,
        help="Size (in GB) of the random test file to generate. Default: 5.",
    )
    parser.add_argument(
        "--azcopy-path",
        default=os.environ.get("AZCOPY_PATH", "azcopy"),
        help="Path to the azcopy executable. Default: 'azcopy' (must be on PATH), "
        "or set AZCOPY_PATH.",
    )
    parser.add_argument(
        "--container-name",
        default="samplecontainer",
        help="Blob container name created in each storage account. "
        "Default: samplecontainer.",
    )
    parser.add_argument(
        "--file-path",
        default="./sample_file",
        help="Local path for the generated test file. Default: ./sample_file.",
    )
    parser.add_argument(
        "--output-csv",
        default="performance_data.csv",
        help="Where to write the CSV results. Default: performance_data.csv.",
    )
    parser.add_argument(
        "--log-file",
        default="performance_log.txt",
        help="Where to write the run log. Default: performance_log.txt.",
    )
    parser.add_argument(
        "--skip-create",
        action="store_true",
        help="Skip creating storage accounts/containers (use if they already "
        "exist from a previous run) and go straight to the transfer tests.",
    )
    return parser.parse_args()


def create_storage_accounts(args):
    """Create one storage account + container per region and return their
    blob container URLs, keyed by region."""
    storage_accounts = {}
    for region in args.regions:
        account_name = f"{args.storage_prefix}{region}"

        create_account_command = [
            "az", "storage", "account", "create",
            "--name", account_name,
            "--resource-group", args.resource_group,
            "--location", region,
            "--sku", "Standard_LRS",
        ]
        subprocess.run(create_account_command, check=True)
        logging.info("Created storage account %s in region %s", account_name, region)

        # Fetch the account key so we can create the container without
        # relying on RBAC propagation delay.
        get_key_command = [
            "az", "storage", "account", "keys", "list",
            "--account-name", account_name,
            "--resource-group", args.resource_group,
            "--query", "[0].value",
            "--output", "tsv",
        ]
        account_key = subprocess.check_output(get_key_command, text=True).strip()
        logging.info("Retrieved key for storage account %s", account_name)

        create_container_command = [
            "az", "storage", "container", "create",
            "--name", args.container_name,
            "--account-name", account_name,
            "--account-key", account_key,
        ]
        subprocess.run(create_container_command, check=True)
        logging.info("Created container %s in storage account %s", args.container_name, account_name)

        if args.assignee_object_id:
            assign_role_command = [
                "az", "role", "assignment", "create",
                "--role", "Storage Blob Data Contributor",
                "--assignee", args.assignee_object_id,
                "--scope",
                f"/subscriptions/{args.subscription_id}/resourceGroups/"
                f"{args.resource_group}/providers/Microsoft.Storage/storageAccounts/{account_name}",
            ]
            subprocess.run(assign_role_command, check=True)
            logging.info(
                "Assigned 'Storage Blob Data Contributor' to %s for storage account %s",
                args.assignee_object_id, account_name,
            )

        storage_accounts[region] = f"https://{account_name}.blob.core.windows.net/{args.container_name}"
    return storage_accounts


def generate_file(file_path, size_gb):
    """Write a file of random bytes at file_path, size_gb gigabytes large."""
    print(f"Generating {size_gb}GB file with random content...")
    logging.info("Generating %sGB file with random content at %s", size_gb, file_path)
    with open(file_path, "wb") as f:
        for _ in range(size_gb * 1024):
            f.write(os.urandom(1024 * 1024))  # write 1MB of random data at a time
    print("File generated.")
    logging.info("File generated.")


def azcopy_copy(azcopy_path, source, destination, file_size_gb):
    """Run `azcopy copy` and return (elapsed_seconds, throughput_mb_per_sec),
    or (None, None) if the copy failed."""
    start_time = time.time()
    result = subprocess.run(
        [azcopy_path, "copy", source, destination, "--overwrite=true"],
        capture_output=True, text=True,
    )
    end_time = time.time()
    print(result.stdout)

    if result.returncode != 0:
        error_message = f"Error during AzCopy: {result.stderr}"
        print(error_message)
        logging.error(error_message)
        return None, None

    total_time = end_time - start_time
    file_size_mb = file_size_gb * 1024
    mb_per_sec = file_size_mb / total_time
    return total_time, mb_per_sec


def main():
    args = parse_args()
    logging.basicConfig(filename=args.log_file, level=logging.INFO, format="%(asctime)s - %(message)s")

    if args.skip_create:
        regions_urls = {
            region: f"https://{args.storage_prefix}{region}.blob.core.windows.net/{args.container_name}"
            for region in args.regions
        }
    else:
        regions_urls = create_storage_accounts(args)

    if not os.path.exists(args.file_path):
        generate_file(args.file_path, args.file_size_gb)

    # Authenticate azcopy with the current Azure CLI/managed identity session.
    result = subprocess.run([args.azcopy_path, "login", "--identity"], capture_output=True, text=True)
    print(result.stderr)
    print(result.stdout)

    performance_data = []

    # Local -> each region.
    for region, destination_url in regions_urls.items():
        print(f"Copying to {region}...")
        logging.info("Copying to %s...", region)
        total_time, mb_per_sec = azcopy_copy(
            args.azcopy_path, args.file_path,
            f"{destination_url}/{os.path.basename(args.file_path)}",
            args.file_size_gb,
        )
        if total_time is not None:
            performance_data.append(["local", region, total_time, mb_per_sec])
            print(f"Copied to {region} in {total_time:.2f} seconds ({mb_per_sec:.2f} MB/sec)")
            logging.info("Copied to %s in %.2f seconds (%.2f MB/sec)", region, total_time, mb_per_sec)

    # Region -> region (including same-region intra-region baseline).
    for src_region, src_url in regions_urls.items():
        for dst_region, dst_url in regions_urls.items():
            source_file_url = f"{src_url}/{os.path.basename(args.file_path)}"
            if src_region != dst_region:
                print(f"Copying from {src_region} to {dst_region}...")
                logging.info("Copying from %s to %s...", src_region, dst_region)
                destination_file_url = f"{dst_url}/{os.path.basename(args.file_path)}"
                label = dst_region
            else:
                # Copy within the same region (to a different blob name) to
                # measure intra-region performance.
                print(f"Copying within {src_region} to measure intra-region performance...")
                logging.info("Copying within %s to measure intra-region performance...", src_region)
                destination_file_url = f"{dst_url}/{os.path.basename(args.file_path)}_dst"
                label = f"{src_region} (intra-region)"

            total_time, mb_per_sec = azcopy_copy(
                args.azcopy_path, source_file_url, destination_file_url, args.file_size_gb
            )
            if total_time is not None:
                performance_data.append([src_region, label, total_time, mb_per_sec])
                print(f"Copied {src_region} -> {label} in {total_time:.2f} seconds ({mb_per_sec:.2f} MB/sec)")
                logging.info("Copied %s -> %s in %.2f seconds (%.2f MB/sec)", src_region, label, total_time, mb_per_sec)

    df = pd.DataFrame(performance_data, columns=["from", "to", "Total Time (s)", "MB/sec"])
    df.to_csv(args.output_csv, index=False)
    print(f"Performance data saved to {args.output_csv}")
    logging.info("Performance data saved to %s", args.output_csv)


if __name__ == "__main__":
    main()
