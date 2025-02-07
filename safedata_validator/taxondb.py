"""The taxondb module.

This module contains functions to:

1. download versions of the GBIF backbone taxonomy and NCBI taxonomy databases, and
2. build and index SQLite3 databases of those datasets for use in local taxon
   validation.

The functions also store the timestamp of each database version in the SQLite3 files, so
that datasets can include a taxonomy timestamp.
"""

import csv
import ftplib
import gzip
import os
import shutil
import sqlite3
import zipfile
from io import TextIOWrapper

import requests
from dateutil.parser import isoparse, parse
from tqdm.auto import tqdm

from safedata_validator.logger import FORMATTER, LOGGER, log_and_raise


def download_gbif_backbone(outdir: str, timestamp: str = None) -> dict:
    """Download the GBIF backbone database.

    This function downloads the data for a GBIF backbone taxonomy version to a given
    location. By default, the most recent version ('current') is downloaded. A timestamp
    (e.g. '2021-11-26') can be provided to select a particular version from the list at:

        https://hosted-datasets.gbif.org/datasets/backbone/.

    Args:
        outdir: The location to download the files to
        timestamp: The timestamp for an available GBIF backbone version.

    Returns:
        A dictionary giving the paths to the downloaded files and timestamp of the
        version downloaded.
    """

    LOGGER.info(f"Downloading GBIF data to: {outdir}")
    FORMATTER.push()
    return_dict = {}

    # Get the timestamp of the current database if none is provided - could just use
    # 'current', but need a reliable source of the date to use in a filename.
    if timestamp is None:
        gbif_backbone = (
            "https://api.gbif.org/v1/dataset/d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
        )
        timestamp = requests.get(gbif_backbone)

        if not timestamp.ok:
            log_and_raise(
                "Could not read current backbone timestamp from GBIF API", IOError
            )

        timestamp = timestamp.json()["pubDate"]

    # Validate the timestamp
    try:
        timestamp = isoparse(timestamp)
    except ValueError:
        log_and_raise(f"Could not parse timestamp: {timestamp}", ValueError)

    # Render timestamp as ISO date
    timestamp_fmt = timestamp.date().isoformat()
    return_dict["timestamp"] = timestamp_fmt

    # Try and download the files - check heads to make sure files exists
    LOGGER.info(f"Checking for version with timestamp {timestamp_fmt}")

    url = f"https://hosted-datasets.gbif.org/datasets/backbone/{timestamp_fmt}/"
    simple_head = requests.head(url + "simple.txt.gz")
    deleted_head = requests.head(url + "simple-deleted.txt.gz")

    if not simple_head.ok:
        log_and_raise(
            "Backbone file not found. Check the available timestamps at: \n"
            "    https://hosted-datasets.gbif.org/datasets/backbone",
            ValueError,
        )

    deleted_exists = deleted_head.ok

    # Download files to target directory
    targets = [("simple", "simple.txt.gz", int(simple_head.headers["Content-Length"]))]

    if deleted_exists:
        targets += [
            (
                "deleted",
                "simple-deleted.txt.gz",
                int(deleted_head.headers["Content-Length"]),
            )
        ]

    for key, file, fsize in targets:
        # Download the file with a TQDM progress bar
        LOGGER.info(f"Downloading {file}")
        file_req = requests.get(url + file, stream=True)
        out_path = os.path.join(outdir, file)
        with tqdm.wrapattr(file_req.raw, "read", total=fsize) as r_raw:
            with open(out_path, "wb") as outf:
                shutil.copyfileobj(r_raw, outf)

        # store the file path
        return_dict[key] = out_path

    FORMATTER.pop()

    return return_dict


def build_local_gbif(
    outdir: str, timestamp: str, simple: str, deleted: str = None, keep: bool = False
) -> None:
    """Create a local GBIF backbone database.

    This function takes the paths to downloaded data files for the GBIF backbone
    taxonomy and builds a SQLite3 database file for use in local validation in the
    safedata_validator package. The database file is created in the provided outdir
    location with the name 'gbif_backbone_timestamp.sqlite'. The location of this file
    then needs to be included in the package configuration to be used in validation.

    The data files can be downloaded using the download_gbif_backbone function and two
    files can be used. The main data is in 'simple.txt.gz' but deleted taxa can also be
    included from 'simple-deleted.txt.gz'. Data is read automatically from the
    compressed files - they do not need to be extracted.

    By default, the downloaded files are deleted after the database has been created,
    but the 'keep' argument can be used to retain them.

    Args:
        outdir: The location to create the SQLite file
        timestamp: The timestamp of the downloaded version.
        simple: The path to the simple.txt.gz file.
        deleted: The path to the simple-deleted.txt.gz
        keep: Should the original datafiles be retained.
    """

    # Create the output file and turn off safety features for speed
    outfile = os.path.join(outdir, f"gbif_backbone_{timestamp}.sqlite")
    LOGGER.info(f"Building GBIF backbone database in: {outfile}")
    FORMATTER.push()

    con = sqlite3.connect(outfile)
    con.execute("PRAGMA synchronous = OFF")

    # Write the timestamp into a table
    con.execute("CREATE TABLE timestamp (timestamp date);")
    con.execute(f"INSERT INTO timestamp VALUES ('{timestamp}');")
    con.commit()
    LOGGER.info("Timestamp table created")

    # Create the schema for the backbone table, using drop fields to remove unwanted
    # fields in the schema and the data tuples. The file_schema list describes the full
    # set of fields provided by GBIF
    file_schema = [
        ("id", "int PRIMARY KEY"),
        ("parent_key", "int"),
        ("basionym_key", "int"),
        ("is_synonym", "boolean"),
        ("status", "text"),
        ("rank", "text"),
        ("nom_status", "text[]"),
        ("constituent_key", "text"),
        ("origin", "text"),
        ("source_taxon_key", "int"),
        ("kingdom_key", "int"),
        ("phylum_key", "int"),
        ("class_key", "int"),
        ("order_key", "int"),
        ("family_key", "int"),
        ("genus_key", "int"),
        ("species_key", "int"),
        ("name_id", "int"),
        ("scientific_name", "text"),
        ("canonical_name", "text"),
        ("genus_or_above", "text"),
        ("specific_epithet", "text"),
        ("infra_specific_epithet", "text"),
        ("notho_type", "text"),
        ("authorship", "text"),
        ("year", "text"),
        ("bracket_authorship", "text"),
        ("bracket_year", "text"),
        ("name_published_in", "text"),
        ("issues", "text[]"),
    ]

    drop_fields = ["name_published_in", "issues"]

    # Get a logical index of which fields are being kept
    drop_index = [True if vl[0] in drop_fields else False for vl in file_schema]

    # Create the final schema for the backbone table, including a new field to show
    # deleted taxa and insert statements for both the backbone and deleted files
    output_schema = ", ".join(
        [" ".join(val) for val, drop in zip(file_schema, drop_index) if not drop]
    )
    output_schema = f"CREATE TABLE backbone ({output_schema}, deleted boolean)"

    insert_placeholders = ",".join(["?"] * (len(drop_index) - sum(drop_index)))
    insert_statement = f"INSERT INTO backbone VALUES ({insert_placeholders}, False)"

    insert_deleted_statement = (
        f"INSERT INTO backbone VALUES ({insert_placeholders}, True)"
    )

    # Create the table
    con.execute(output_schema)
    con.commit()
    LOGGER.info("Backbone table created")

    # Import data from the simple backbone and deleted taxa

    # The approach below is more efficient but makes it impossible to drop fields and
    # substitute \\N to None. Although converting \\N to None can be done later with an
    # update statement, you _cannot_ drop fields in sqlite3, so that has to be done up
    # front.
    #
    # con.executemany( insert_statement, bb_reader )

    LOGGER.info("Adding core backbone taxa")

    with gzip.open(simple, "rt") as bbn:

        # The files are tab delimited but the quoting is sometimes unclosed,
        # so turning off quoting - includes quotes in the fields where present
        bb_reader = csv.reader(bbn, delimiter="\t", quoting=csv.QUOTE_NONE)

        # There is no obvious way of finding the number of rows in simple.txt without
        # reading the file and counting them. And that is a huge cost just to provide a
        # progress bar with real percentages, so just show a progress meter to show
        # things happening
        with tqdm(total=None) as pbar:

            # Loop over the lines in the file.
            for row in bb_reader:
                row = [
                    None if val == "\\N" else val
                    for val, drp in zip(row, drop_index)
                    if not drp
                ]

                con.execute(insert_statement, row)
                pbar.update()

        con.commit()

    if deleted is not None:
        LOGGER.info("Adding deleted taxa")

        with gzip.open(deleted, "rt") as dlt:

            # The files are tab delimited but the quoting is sometimes unclosed,
            # so turning off quoting - includes quotes in the fields where present
            dl_reader = csv.reader(dlt, delimiter="\t", quoting=csv.QUOTE_NONE)

            with tqdm(total=None) as pbar:
                for row in dl_reader:
                    row = [
                        None if val == "\\N" else val
                        for val, drp in zip(row, drop_index)
                        if not drp
                    ]

                    con.execute(insert_deleted_statement, row)
                    pbar.update()

        con.commit()

    # Create the indices
    LOGGER.info("Creating database indexes")

    con.execute("CREATE INDEX backbone_name_rank ON backbone (canonical_name, rank);")
    con.execute("CREATE INDEX backbone_id ON backbone (id);")
    con.commit()

    # Delete the downloaded files
    if not keep:
        LOGGER.info("Removing downloaded files")
        os.remove(simple)
        if deleted is not None:
            os.remove(deleted)

    FORMATTER.pop()


def download_ncbi_taxonomy(outdir: str, timestamp: str = None) -> dict:
    """Download the NCBI taxonomy database.

    This function downloads the data for the NCBI taxonomy to a given location. By
    default, the most recent version is downloaded. A timestamp (e.g. '2021-11-26') can
    be provided to select a particular version from the following link.

        https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump_archive/

    Args:
        outdir: The location to download the files to
        timestamp: The timestamp for an available NCBI taxonomy version.

    Returns:
        A dictionary giving the paths to the downloaded files and timestamp of the
        version downloaded.
    """

    LOGGER.info(f"Downloading NCBI data to: {outdir}")
    FORMATTER.push()
    return_dict = {}

    # Get the available versions and metadata via FTP.
    try:
        LOGGER.info("Connecting to FTP server")
        ftp = ftplib.FTP(host="ftp.ncbi.nlm.nih.gov")
        ftp.login()
        ftp.cwd("pub/taxonomy/taxdump_archive/")
        available_versions = list(ftp.mlsd("."))
    except ftplib.all_errors:
        log_and_raise("Could not retrieve available versions", IOError)

    # Get a list of filename, date tuples
    versions = [
        (fnm, parse(md["modify"]).date().isoformat(), md["size"])
        for fnm, md in available_versions
        if fnm.startswith("taxdmp")
    ]
    versions.sort(key=lambda val: val[1], reverse=True)

    if timestamp is None:
        # Get most recent version
        file, timestamp, total = versions[0]
        LOGGER.info(f"Using most recent archive: {timestamp}")
    else:
        # Find the timestamp in the list
        fnames, dates, sizes = zip(*versions)
        if timestamp not in dates:
            log_and_raise(f"No version found with timestamp: {timestamp}", ValueError)
        idx = dates.index(timestamp)
        file = fnames[idx]
        total = sizes[idx]

    return_dict["timestamp"] = timestamp

    # Retrieve the requested file, using a callback wrapper to track progress
    out_file = os.path.join(outdir, file)
    LOGGER.info(f"Downloading taxonomy to: {out_file}")

    with open(out_file, "wb") as outf:

        with tqdm(
            total=int(total),
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
        ) as pbar:

            def _callback(data):
                data_len = len(data)
                pbar.update(data_len)
                outf.write(data)

            ftp.retrbinary(f"RETR {file}", _callback)

    ftp.close()

    # store the file path
    return_dict["taxdmp"] = out_file

    FORMATTER.pop()

    return return_dict


def build_local_ncbi(
    outdir: str, timestamp: str, taxdmp: str, keep: bool = False
) -> None:
    """Create a local NCBI taxonomy database.

    This function takes the path to a downloaded data file from the NCBI Taxonomy
    archive and builds a SQLite3 database file for use in local validation in the
    safedata_validator package. The database file is created in the provided outdir
    location with the name 'ncbi_taxonomy_timestamp.sqlite'. The location of this file
    then needs to be included in the package configuration to be used in validation.

    The data file can be downloaded using the download_ncbi_taxonomy function, which
    will retrieve a `taxdmp_timestamp.zip` archive. Data is read automatically from the
    compressed file - it does not need to be decompressed.

    By default, the downloaded files are deleted after the database has been created,
    but the 'keep' argument can be used to retain them.

    Args:
        outdir: The location to create the SQLite file
        timestamp: The timestamp of the downloaded version.
        taxdmp: The path to the taxdmp ZIP archive.
        keep: Should the original archive be retained.
    """

    # Create the output file
    db_file = os.path.join(outdir, f"ncbi_taxonomy_{timestamp}.sqlite")
    LOGGER.info(f"Building GBIF backbone database in: {db_file}")
    FORMATTER.push()

    # Create the output file and turn off safety features for speed
    con = sqlite3.connect(db_file)
    con.execute("PRAGMA synchronous = OFF")

    # Write the timestamp into a table
    con.execute("CREATE TABLE timestamp (timestamp date);")
    con.execute(f"INSERT INTO timestamp VALUES ('{timestamp}');")
    con.commit()
    LOGGER.info("Timestamp table created")

    # Define the retained fields and schema for each required table
    tables = {
        "nodes": {
            "file": "nodes.dmp",
            "drop": [
                "embl_code",
                "division_id",
                "inherited_div_flag",
                "genetic_code_id",
                "inherited_GC_flag",
                "mito_code_id",
                "inherited_MGC_flag",
                "GenBank_hidden_flag",
                "hidden_subtree_root_flag",
            ],
            "schema": [
                ("tax_id", "int PRIMARY KEY"),
                ("parent_tax_id", "int"),
                ("rank", "text"),
                ("embl_code", "text"),
                ("division_id", "int"),
                ("inherited_div_flag", "boolean"),
                ("genetic_code_id", "int"),
                ("inherited_GC_flag", "boolean"),
                ("mito_code_id", "int"),
                ("inherited_MGC_flag", "boolean"),
                ("GenBank_hidden_flag", "boolean"),
                ("hidden_subtree_root_flag", "boolean"),
                ("comments", "text"),
            ],
        },
        "names": {
            "file": "names.dmp",
            "drop": [],  # ["unique_name"],
            "schema": [
                ("tax_id", "int"),
                ("name_txt", "text"),
                ("unique_name", "text"),
                ("name_class", "text"),
            ],
        },
        "merge": {
            "file": "merged.dmp",
            "drop": [],
            "schema": [("old_tax_id", "int"), ("new_tax_id", "int")],
        },
    }

    archive = zipfile.ZipFile(taxdmp)

    # Process each table
    for tbl, info in tables.items():

        # Get a logical index of which fields are being kept for this table
        drop_index = [True if vl[0] in info["drop"] else False for vl in info["schema"]]

        # Create the final schema for this table
        schema = ", ".join(
            [" ".join(val) for val, drop in zip(info["schema"], drop_index) if not drop]
        )

        schema = f"CREATE TABLE {tbl} ({schema})"

        LOGGER.info(f"Creating {tbl} table")
        con.execute(schema)
        con.commit()

        # Create the insert statement
        placeholders = ",".join(["?"] * (len(drop_index) - sum(drop_index)))
        insert_statement = f"INSERT INTO {tbl} VALUES ({placeholders})"

        # Import data from the archive
        with archive.open(info["file"], "r") as data:

            LOGGER.info(f"Populating {tbl} table from {info['file']}")

            # Use TextIOWrapper to expose the binary data from the Zip as text for CSV
            data_text = TextIOWrapper(data)

            # The files are pipe delimited but the quoting is sometimes unclosed,
            # so turning off quoting - includes quotes in the fields where present
            data_reader = csv.reader(data_text, delimiter="|", quoting=csv.QUOTE_NONE)

            with tqdm(total=None) as pbar:
                for row in data_reader:
                    row = [val.strip() for val, drp in zip(row, drop_index) if not drp]

                    con.execute(insert_statement, row)
                    pbar.update()

        con.commit()

    archive.close()

    # Create the indices
    LOGGER.info("Creating database indexes")
    con.execute("CREATE INDEX node_id ON nodes (tax_id);")
    con.execute("CREATE INDEX all_names ON names (name_txt);")
    con.execute("CREATE INDEX id_name_class ON names (tax_id, name_class);")
    con.execute("CREATE INDEX merged_id ON merge (old_tax_id);")
    con.commit()

    # Delete the downloaded files
    if not keep:
        LOGGER.info("Removing downloaded archive")
        os.remove(taxdmp)

    FORMATTER.pop()
