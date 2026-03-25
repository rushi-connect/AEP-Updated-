"""
spark-submit \
  --deploy-mode client \
  --packages com.databricks:spark-xml_2.12:0.18.0 \
  --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions \
  --conf spark.sql.catalog.spark_catalog=org.apache.iceberg.spark.SparkSessionCatalog \
  --conf spark.sql.catalog.spark_catalog.type=glue \
  --conf spark.sql.catalog.spark_catalog.glue.id=889415020100 \
  --conf spark.sql.catalog.spark_catalog.warehouse=s3://aep-datalake-apps-dev/emr/warehouse/nonvee-uiq-info-oh \
  --conf spark.sql.session.timeZone=America/New_York \
  --conf spark.driver.extraJavaOptions=-Duser.timezone=America/New_York \
  --conf spark.executor.extraJavaOptions=-Duser.timezone=America/New_York \
  --conf spark.sql.legacy.timeParserPolicy=LEGACY \
  --driver-cores 4 \
  --driver-memory 32g \
  --conf spark.driver.maxResultSize=16g \
  --executor-cores 1 \
  --executor-memory 5g \
  s3://aep-datalake-apps-dev/hdpapp/aep_dl_util_ami_nonvee_info/pyspark/nonvee_uiq_info_job.py \
  --job_name nonvee-uiq-info-oh \
  --opco oh \
  --data_bucket aep-datalake-work-dev \
  --batch_start_dttm_str "2024-10-25 00:00:00" \
  --batch_end_dttm_str "2024-10-26 00:00:00" \
  --batch_run_dttm_str "2024-10-26 00:00:00"
"""
import time
import uuid
import boto3
import argparse

from typing import Dict
from textwrap import dedent
from functools import reduce
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pyspark.sql import SparkSession
from pyspark.sql.window import Window
from pyspark.sql.utils import AnalysisException

import pyspark.sql.functions as F


data_prefix = "raw/intervals/nonvee/uiq_info"
marker_file_name = "index.txt"
marker_file_max_depth = 2
data_file_ext = ".xml"
data_file_min_size = 0

co_cd_ownr_map = {
    "oh": ["07", "10"],
    "im": ["04"],
    "pso": ["95"],
    "ap": ["01", "02", "06"],
    "swp": ["96"]
}
timezone_map = {
    "oh": "US/Eastern",
    "im": "US/Eastern",
    "pso": "US/Central",
    "ap": "US/Eastern",
    "swp": "US/Eastern"
}

xml_row_tag = "MeterData"
xml_read_mode = "FAILFAST"
xml_infer_schema = "false"
xml_tag_ignore_namespace = "true"
xml_data_timezone = "America/New_York"
xml_tag_value_col_name = "tag_value"

raw_xml_schema_str = """
`_MeterName` STRING,
`_UtilDeviceID` STRING,
`_MacID` STRING,
`IntervalReadData` ARRAY<
    STRUCT<
        `_NumberIntervals`: STRING,
        `_EndTime`: STRING,
        `_StartTime`: STRING,
        `_IntervalLength`: STRING,
        `Interval`: ARRAY<
            STRUCT<
                `_EndTime`: STRING,
                `_BlockSequenceNumber`: STRING,
                `_GatewayCollectedTime`: STRING,
                `_IntervalSequenceNumber`: STRING,
                `Reading`: ARRAY<
                    STRUCT<
                        `_Channel`: STRING,
                        `_RawValue`: STRING,
                        `_Value`: STRING,
                        `_UOM`: STRING,
                        `_BlockEndValue`: STRING
                    >
                >
            >
        >
    >
>
"""

def get_filtered_data_files_map(
    s3_bucket: str,
    s3_prefix: str,
    marker_file_name: str,
    batch_start_dttm_ltz: datetime,
    batch_end_dttm_ltz: datetime,
    marker_file_max_depth: int,
    data_file_min_size: int,
    data_file_ext: str,
) -> Dict:
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    base_prefix_len = len(s3_prefix.strip("/"))

    _time = time.time()
    print(f'===== listing objects in base prefix {s3_prefix} =====')
    i = 0
    j = 0
    k = 0
    all_files_map = defaultdict(list)
    filtered_dirs_prefix = set()
    filtered_files_map = None

    for page in paginator.paginate(Bucket=s3_bucket, Prefix=s3_prefix):
        i += 1
        for obj in page.get('Contents', []):
            j += 1
            obj_key = obj['Key']
            obj_size = obj['Size']
            obj_last_modified = obj['LastModified']
            relative = obj_key[base_prefix_len:]
            relative_depth = relative.count("/")
            level1_dir = relative.strip("/").split("/")[0]
            level1_dir_prefix = f'{s3_prefix}/{level1_dir}'
            file_name = relative.split("/")[-1]

            if (
                obj_key.endswith(f'/{marker_file_name}')
                and batch_start_dttm_ltz <= obj_last_modified < batch_end_dttm_ltz
                and relative_depth <= marker_file_max_depth
            ):
                k += 1
                filtered_dirs_prefix.add(level1_dir_prefix)
            elif obj_size > data_file_min_size and data_file_ext in file_name:
                all_files_map[level1_dir_prefix].append(obj_key)
    print(f'===== found {j} objects in {i} pages =====')
    print(f'completed listing objects in {str(timedelta(seconds=time.time() - _time))}')

    _time = time.time()
    print(f'===== filtering files =====')
    filtered_files_map = {prefix: all_files_map[prefix] for prefix in filtered_dirs_prefix}
    print(f'===== selected {sum(len(f) for f in filtered_files_map.values())} files in {len(filtered_files_map)} dirs =====')
    print(f'completed filtering files in {str(timedelta(seconds=time.time() - _time))}')

    return filtered_files_map


###################################################################################################
# Reference: xfrm_interval_data_to_downstream.py
# The Function delete_old_data is referenced from the script "xfrm_interval_data_to_downstream.py"
###################################################################################################
def delete_old_data(
    spark: SparkSession,
    table_name: str,
    opco: str,
    days: int = 8,
):
    """
    Deletes records older than a specified number of days for a given opco.
    """
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d_%H%M%S")
        delete_query = f"""
        DELETE FROM {table_name}
        WHERE aep_opco = '{opco}' AND run_date < '{cutoff_date}'
        """
        print(f"===== Executing deletion query: {delete_query} =====")
        spark.sql(delete_query)
        print(
            f"===== Data older than {days} days for OPCO '{opco}' deleted successfully. ====="
        )
    except AnalysisException as e:
        print(f"===== ERROR: Failed to delete data from {table_name}: {e} =====")
        raise


def main():
    print(f'===== starting job =====')
    _bgn_time = time.time()

    _time = time.time()
    print(f'===== started parsing job args =====')
    parser = argparse.ArgumentParser()
    parser.add_argument("--job_name", required=True)
    parser.add_argument("--opco", required=True)
    parser.add_argument("--data_bucket", required=True)
    parser.add_argument("--batch_start_dttm_str", required=True)
    parser.add_argument("--batch_end_dttm_str", required=True)
    parser.add_argument("--batch_run_dttm_str", required=True)
    args = parser.parse_args()
    print(f'completed parsing in {str(timedelta(seconds=time.time() - _time))}')
    print(f'===== args [{type(args)}]: {args} =====')

    print(f'===== init vars =====')
    job_name = args.job_name
    opco = args.opco
    data_bucket = args.data_bucket
    batch_start_dttm_str = args.batch_start_dttm_str
    batch_end_dttm_str = args.batch_end_dttm_str
    batch_run_dttm_str = args.batch_run_dttm_str

    _time = time.time()
    print(f'===== creating spark session =====')
    spark = SparkSession.builder \
        .appName(job_name) \
        .enableHiveSupport() \
        .getOrCreate()
    print(f'completed creating spark session in {str(timedelta(seconds=time.time() - _time))}')
    print(f'===== spark application-id: {spark.sparkContext.applicationId} =====')

    current_dttm_ltz = datetime.now(timezone.utc).astimezone()
    local_tz = current_dttm_ltz.tzinfo
    batch_start_dttm_ntz = datetime.strptime(batch_start_dttm_str, "%Y-%m-%d %H:%M:%S")
    batch_start_dttm_ltz = batch_start_dttm_ntz.replace(tzinfo=local_tz)
    batch_end_dttm_ntz = datetime.strptime(batch_end_dttm_str, "%Y-%m-%d %H:%M:%S")
    batch_end_dttm_ltz = batch_end_dttm_ntz.replace(tzinfo=local_tz)
    batch_run_dttm_ntz = datetime.strptime(batch_run_dttm_str, "%Y-%m-%d %H:%M:%S")
    batch_run_dttm_ltz = batch_run_dttm_ntz.replace(tzinfo=local_tz)
    base_prefix = f'{data_prefix}/{opco}'
    scratch_path = f'hdfs:///tmp/scratch/{str(uuid.uuid4())}'
    print(f'===== batch start dttm: {batch_start_dttm_ltz} =====')
    print(f'===== batch end dttm: {batch_end_dttm_ltz} =====')
    print(f'===== batch run dttm: {batch_run_dttm_ltz} =====')
    print(f'===== batch base prefix: {base_prefix} =====')
    print(f'===== batch scratch path: {scratch_path} =====')

    filtered_data_files_map = get_filtered_data_files_map(
        s3_bucket=data_bucket,
        s3_prefix=base_prefix,
        marker_file_name=marker_file_name,
        batch_start_dttm_ltz=batch_start_dttm_ltz,
        batch_end_dttm_ltz=batch_end_dttm_ltz,
        marker_file_max_depth=marker_file_max_depth,
        data_file_min_size=data_file_min_size,
        data_file_ext=data_file_ext,
    )

    # TODO: TEMP - limit to 10 files for testing, remove after testing
    _limited_map = {}
    _remaining = 10
    for _k, _v in filtered_data_files_map.items():
        if _remaining <= 0:
            break
        _limited_map[_k] = _v[:_remaining]
        _remaining -= len(_limited_map[_k])
    filtered_data_files_map = _limited_map
    print(f'===== TEMP: limited to {sum(len(f) for f in filtered_data_files_map.values())} files for testing =====')

    files_count = sum(len(f) for f in filtered_data_files_map.values())
    if files_count == 0:
        print(f'0 files selected. exiting ...')
        spark.stop()
        print(f'completed job in {str(timedelta(seconds=time.time() - _bgn_time))}')
        exit(0)

    _time = time.time()
    print(f'===== reading files =====')
    files_count = 0
    dfs = []
    for k, files in filtered_data_files_map.items():
        files_count += len(files)
        files_str = ",".join([f's3://{data_bucket}/{f}' for f in files])
        df = spark.read.format("com.databricks.spark.xml") \
            .option("rowTag", xml_row_tag) \
            .option("mode", xml_read_mode) \
            .option("inferSchema", xml_infer_schema) \
            .option("valueTag", xml_tag_value_col_name) \
            .option("ignoreNamespace", xml_tag_ignore_namespace) \
            .option("timezone", xml_data_timezone) \
            .schema(raw_xml_schema_str) \
            .load(files_str)
        dfs.append(df)
    print(f'===== read {files_count} files in {len(dfs)} dirs =====')
    df_raw = reduce(lambda a, b: a.union(b), dfs)
    print(f'completed reading in {str(timedelta(seconds=time.time() - _time))}')
    df_raw.printSchema()

    _time = time.time()
    print(f'===== staging raw data =====')
    raw_staging_path = f'{scratch_path}/raw_stage/'
    df_raw.coalesce(1000).write.mode("overwrite").parquet(raw_staging_path)
    print(f'completed staging in {str(timedelta(seconds=time.time() - _time))}')
    staged_raw_df = spark.read.parquet(raw_staging_path)
    staged_raw_df.printSchema()

    _time = time.time()
    print(f'===== counting staged_df records =====')
    staged_raw_df_count = staged_raw_df.count()
    staged_raw_df_partition_count = staged_raw_df.rdd.getNumPartitions()
    print(f'counted {staged_raw_df_count} record(s) in {staged_raw_df_partition_count} partition(s)')
    print(f'completed counting in {str(timedelta(seconds=time.time() - _time))}')

    # read macs_ami and uom_mapping tables
    ######################################################################
    # Reference: stg_nonvee.interval_data_files_{opco}_src_vw
    # Source: scripts/ddl/stg_nonvee.interval_data_files_{opco}_src_vw.ddl
    # Called by: scripts/source_interval_data_files.sh
    # UOM Mapping Reference Table
    ######################################################################
    _time = time.time()
    print(f'===== reading stg_nonvee.meter_premise_macs_ami =====')
    macs_df = spark.table("stg_nonvee.meter_premise_macs_ami").filter(
        F.col("co_cd_ownr").isin(co_cd_ownr_map[opco])
    ).select(
        F.col("prem_nb"),
        F.col("bill_cnst"),
        F.col("acct_clas_cd"),
        F.col("acct_type_cd"),
        F.col("devc_cd"),
        F.col("pgm_id_nm"),
        F.concat(
            F.coalesce(F.col("tx_mand_data"), F.lit("")),
            F.coalesce(F.col("doe_nb"), F.lit("")),
            F.coalesce(F.col("serv_deliv_id"), F.lit("")),
        ).cast("string").alias("sd"),
        F.col("mfr_devc_ser_nbr"),
        F.col("mtr_inst_ts"),
        F.col("mtr_rmvl_ts"),
        F.col("acct_turn_on_dt"),
        F.col("acct_turn_off_dt"),
        F.col("bill_acct_nb"),
        F.col("mtr_pnt_nb"),
        F.col("tarf_pnt_nb"),
        F.lit(None).cast("string").alias("cmsg_mtr_mult_nb"),
        F.unix_timestamp("mtr_inst_ts", "yyyy-MM-dd HH:mm:ss").alias("unix_mtr_inst_ts"),
        F.when(
            F.col("mtr_rmvl_ts") == "9999-01-01",
            F.unix_timestamp("mtr_rmvl_ts", "yyyy-MM-dd")
        ).otherwise(
            F.unix_timestamp("mtr_rmvl_ts", "yyyy-MM-dd HH:mm:ss")
        ).alias("unix_mtr_rmvl_ts"),
        F.unix_timestamp("acct_turn_on_dt", "yyyy-MM-dd").alias("unix_acct_turn_on_dt"),
        F.unix_timestamp("acct_turn_off_dt", "yyyy-MM-dd").alias("unix_acct_turn_off_dt"),
        F.col("serv_city_ad").alias("aep_city"),
        F.col("serv_zip_ad").alias("aep_zip"),
        F.col("state_cd").alias("aep_state"),
    )
    print(f'completed reading in {str(timedelta(seconds=time.time() - _time))}')
    macs_df.printSchema()

    ######################################################################
    # Reference: stg_nonvee.interval_data_files_{opco}_src_vw
    # Source: scripts/ddl/stg_nonvee.interval_data_files_{opco}_src_vw.ddl
    # Called by: scripts/source_interval_data_files.sh
    # UOM Mapping Reference Table
    ######################################################################
    _time = time.time()
    print(f'===== reading usage_nonvee.uom_mapping =====')
    uom_mapping_df = spark.table("usage_nonvee.uom_mapping").filter(
        F.col("aep_opco") == opco
    ).select(
        F.col("aep_channel_id"),
        F.col("aep_derived_uom"),
        F.col("name_register"),
        F.col("aep_srvc_qlty_idntfr"),
        F.col("aep_raw_uom"),
        F.col("value_mltplr_flg"),
    ).distinct()
    print(f'completed reading in {str(timedelta(seconds=time.time() - _time))}')
    uom_mapping_df.printSchema()

    print(f'===== Step 1: Rename columns =====')
    interval_data_files_src_df = staged_raw_df.withColumnRenamed(
        "_MeterName", "metername"
    ).withColumnRenamed(
        "_UtilDeviceID", "utildeviceid"
    ).withColumnRenamed(
        "_MacID", "macid"
    ).withColumnRenamed(
        "IntervalReadData", "interval_reading"
    )
    interval_data_files_src_df.printSchema()

    ###############################################################################
    # Reference: stg_nonvee.interval_data_files_{opco}_src_vw
    # Source: scripts/ddl/stg_nonvee.interval_data_files_{opco}_src_vw.ddl
    # Called by: scripts/source_interval_data_files.sh
    # 1st EXPLODE: Flatten interval_reading array
    ###############################################################################
    print(f'===== Step 2: 1st EXPLODE (intervals) =====')
    interval_exploded_df = interval_data_files_src_df.withColumn(
        "exp_ird", F.explode("interval_reading")
    ).withColumn(
        "exp_interval", F.explode("exp_ird.Interval")
    ).select(
        F.col("metername").alias("MeterName"),
        F.col("utildeviceid").alias("UtilDeviceID"),
        F.col("macid").alias("MacID"),
        F.col("exp_ird._IntervalLength").alias("IntervalLength"),
        F.col("exp_ird._StartTime").alias("blockstarttime"),
        F.col("exp_ird._EndTime").alias("blockendtime"),
        F.col("exp_ird._NumberIntervals").alias("NumberIntervals"),
        F.col("exp_interval._EndTime").alias("int_endtime"),
        F.col("exp_interval._GatewayCollectedTime").alias("int_gatewaycollectedtime"),
        F.col("exp_interval._BlockSequenceNumber").alias("int_blocksequencenumber"),
        F.col("exp_interval._IntervalSequenceNumber").alias("int_intervalsequencenumber"),
        F.col("exp_interval.Reading").alias("int_reading"),
    ).filter(
        F.date_format(
            F.to_timestamp(F.substring("blockstarttime", 1, 10), "yyyy-MM-dd"),
            "yyyyMMdd",
        ) >= F.lit("20150301")
    )
    interval_exploded_df.printSchema()

    ###############################################################################
    # 2nd EXPLODE: Flatten int_reading + time calculations
    ###############################################################################
    print(f'===== Step 3: 2nd EXPLODE (readings) =====')
    reading_exploded_df = interval_exploded_df.withColumn(
        "exp_reading", F.explode("int_reading")
    ).select(
        F.col("MeterName"),
        F.col("UtilDeviceID"),
        F.col("MacID"),
        F.col("IntervalLength"),
        F.col("blockstarttime"),
        F.col("blockendtime"),
        F.col("NumberIntervals"),
        F.from_unixtime(
            F.unix_timestamp(F.substring("int_endtime", 1, 19), "yyyy-MM-dd'T'HH:mm:ss")
            - (F.col("IntervalLength").cast("int") * 60),
            "yyyy-MM-dd'T'HH:mm:ss",
        ).alias("starttime"),
        F.from_unixtime(
            F.unix_timestamp(
                F.substring("int_endtime", 1, 19), "yyyy-MM-dd'T'HH:mm:ss"
            ), "yyyy-MM-dd'T'HH:mm:ss",
        ).alias("endtime"),
        F.substring("int_endtime", -6, 6).alias("interval_epoch"),
        F.col("int_gatewaycollectedtime"),
        F.col("int_blocksequencenumber"),
        F.col("int_intervalsequencenumber"),
        F.col("exp_reading._Channel").alias("channel"),
        F.col("exp_reading._RawValue").alias("rawvalue"),
        F.col("exp_reading._Value").alias("value"),
        F.col("exp_reading._UOM").alias("uom"),
    ).filter(
        F.isnotnull("channel")
        & F.isnotnull("rawvalue")
        & F.isnotnull("value")
        & F.isnotnull("uom")
    )
    reading_exploded_df.printSchema()

    ###############################################################################
    # Reference: stg_nonvee.interval_data_files_oh_src_vw.ddl
    # LEFT JOIN with uom_mapping on reading.channel = um.aep_channel_id
    ###############################################################################
    print(f'===== Step 4: LEFT JOIN with UOM mapping =====')
    interval_data_files_src_vw_df = reading_exploded_df.alias("reading").join(
        uom_mapping_df.alias("um"),
        on=(F.col("reading.channel") == F.col("um.aep_channel_id")),
        how="left",
    ).select(
        F.trim(F.col("reading.MeterName")).alias("MeterName"),
        F.col("reading.UtilDeviceID"),
        F.col("reading.MacID"),
        F.col("reading.IntervalLength"),
        F.col("reading.blockstarttime"),
        F.col("reading.blockendtime"),
        F.col("reading.NumberIntervals"),
        F.concat(
            F.col("reading.starttime"),
            F.col("reading.interval_epoch"),
        ).cast("string").alias("starttime"),
        F.concat(
            F.col("reading.endtime"),
            F.col("reading.interval_epoch"),
        ).cast("string").alias("endtime"),
        F.col("reading.interval_epoch"),
        F.col("reading.int_gatewaycollectedtime"),
        F.col("reading.int_blocksequencenumber"),
        F.col("reading.int_intervalsequencenumber"),
        F.col("reading.channel"),
        F.col("reading.rawvalue"),
        F.col("reading.value"),
        F.lower(F.trim(F.col("reading.uom"))).alias("uom"),
        F.coalesce(F.col("um.aep_derived_uom"), F.lit("UNK")).cast("string").alias("aep_uom"),
        F.when(
            F.isnotnull("um.name_register"),
            F.expr("translate(um.name_register, 'mtr_ivl_len', reading.IntervalLength)"),
        ).otherwise(F.lit("UNK")).cast("string").alias("name_register"),
        F.coalesce(F.col("um.aep_srvc_qlty_idntfr"), F.lit("UNK")).cast("string").alias("aep_sqi"),
        F.col("um.value_mltplr_flg"),
        F.unix_timestamp(F.concat(F.col("reading.endtime"), F.col("reading.interval_epoch")), "yyyy-MM-dd'T'HH:mm:ssXXX").alias("unix_endtime"),
    )
    interval_data_files_src_vw_df.printSchema()

    ###############################################################################
    # Reference: stg_nonvee.interval_data_files_{opco}_stg
    # Source: scripts/stage_interval_xml_files.sh (Lines 194-272)
    #         scripts/ddl/stg_nonvee.interval_data_files_{opco}_stg.ddl
    # LEFT JOIN on metername + time window validation with MACS
    # LOGIC:
    # 1. Read from interval_data_files_{opco}_src_vw_df
    # 2. Read MACS reference table from Glue catalog
    # 3. LEFT JOIN on metername + time window validation
    # 4. Rename/transform columns to match DDL (41 columns)
    # 5. Load MACS reference table
    ###############################################################################
    print(f'===== Step 5: LEFT JOIN with MACS =====')
    interval_data_files_stg_df = interval_data_files_src_vw_df.alias("src").join(
        macs_df.alias("macs"),
        on=(
            (F.col("src.MeterName") == F.col("macs.mfr_devc_ser_nbr"))
            & F.col("src.unix_endtime").between(
                F.col("macs.unix_mtr_inst_ts"), F.col("macs.unix_mtr_rmvl_ts"),
            )
            & F.col("src.unix_endtime").between(
                F.col("macs.unix_acct_turn_on_dt"), F.col("macs.unix_acct_turn_off_dt"),
            )
        ),
        how="left",
    ).select(
        F.lit(opco).cast("string").alias("aep_opco"),
        F.col("src.MeterName").alias("serialnumber"),
        F.col("src.UtilDeviceID").alias("utildeviceid"),
        F.col("src.MacID").alias("macid"),
        F.col("src.IntervalLength").alias("intervallength"),
        F.col("src.blockstarttime"),
        F.col("src.blockendtime"),
        F.col("src.NumberIntervals").alias("numberintervals"),
        F.col("src.starttime"),
        F.col("src.endtime"),
        F.col("src.interval_epoch"),
        F.col("src.int_gatewaycollectedtime"),
        F.col("src.int_blocksequencenumber"),
        F.col("src.int_intervalsequencenumber"),
        F.col("src.channel"),
        F.col("src.value").alias("aep_raw_value"),
        F.col("src.value"),
        F.col("src.uom").alias("aep_raw_uom"),
        F.upper(F.col("src.aep_uom")).alias("aep_derived_uom"),
        F.col("src.name_register"),
        F.col("src.aep_sqi").alias("aep_srvc_qlty_idntfr"),
        F.col("macs.prem_nb").alias("aep_premise_nb"),
        F.col("macs.bill_cnst"),
        F.col("macs.acct_clas_cd").alias("aep_acct_cls_cd"),
        F.col("macs.acct_type_cd").alias("aep_acct_type_cd"),
        F.col("macs.devc_cd").alias("aep_devicecode"),
        F.col("macs.pgm_id_nm").alias("aep_meter_program"),
        F.col("macs.sd").alias("aep_srvc_dlvry_id"),
        F.col("macs.mtr_pnt_nb").alias("aep_mtr_pnt_nb"),
        F.col("macs.tarf_pnt_nb").alias("aep_tarf_pnt_nb"),
        F.col("macs.cmsg_mtr_mult_nb").alias("aep_comp_mtr_mltplr"),
        F.col("macs.mtr_rmvl_ts").alias("aep_mtr_removal_ts"),
        F.col("macs.mtr_inst_ts").alias("aep_mtr_install_ts"),
        F.col("macs.aep_city"),
        F.substring(F.col("macs.aep_zip"), 1, 5).cast("string").alias("aep_zip"),
        F.col("macs.aep_state"),
        F.lit("info-insert").cast("string").alias("hdp_update_user"),
        F.col("src.value_mltplr_flg"),
        F.substring(F.trim(F.col("src.MeterName")), -2, 2).cast("string").alias("aep_meter_bucket"),
    )
    interval_data_files_stg_df.printSchema()

    #######################################################################################################
    # Source: stg_nonvee.interval_data_files_{opco}_stg_vw.ddl
    # Called by: xfrm_interval_data_files.sh (Line 190)
    # Input: interval_data_files_stg_df (40 columns)
    # Output: interval_data_files_stg_vw_df (43 columns)
    #
    # Transforms staged data into STG_VW equivalent:
    # - Literals: source, isvirtual_meter/register, usage_type, timezone
    # - aep_service_point = concat(premise, tarf, mtr) when all non-null
    # - aep_sec_per_intrvl = intervallength(int) * 60 (DDL Line 22)
    # - value = value * bill_cnst when value_mltplr_flg='Y' (DDL Lines 29-32)
    #   cast to double here (Hive implicit), float cast in Step 8 (xfrm_interval_data_files.sh Line 168)
    # - aep_comp_mtr_mltplr cast to double (DDL Line 39)
    # - aep_endtime_utc = unix_timestamp(endtime+epoch) cast to string (DDL Line 40, XFRM DDL Line 38)
    # - hdp_insert/update_dttm = current_timestamp, timestamp cast in Step 8 (xfrm Lines 182-183)
    # - authority = substr(aep_premise_nb, 1, 2), aep_usage_dt = substr(starttime, 1, 10)
    # #######################################################################################################
    print(f'===== Step 6: STG_VW transformations =====')
    interval_data_files_stg_vw_df = interval_data_files_stg_df.select(
        F.col("serialnumber"),
        F.lit("nonvee-hes").cast("string").alias("source"),
        F.col("aep_devicecode"),
        F.lit("N").cast("string").alias("isvirtual_meter"),
        F.col("interval_epoch").alias("timezoneoffset"),
        F.col("aep_premise_nb"),
        F.when(
            F.isnotnull("aep_premise_nb")
            & F.isnotnull("aep_tarf_pnt_nb")
            & F.isnotnull("aep_mtr_pnt_nb"),
            F.concat(
                F.col("aep_premise_nb"),
                F.lit("-"),
                F.col("aep_tarf_pnt_nb"),
                F.lit("-"),
                F.col("aep_mtr_pnt_nb"),
            ),
        ).otherwise(F.lit(None)).cast("string").alias("aep_service_point"),
        F.col("aep_srvc_dlvry_id"),
        F.col("name_register"),
        F.lit("N").cast("string").alias("isvirtual_register"),
        F.col("aep_derived_uom"),
        F.col("aep_raw_uom"),
        F.col("aep_srvc_qlty_idntfr"),
        F.col("channel").alias("aep_channel_id"),
        (F.col("intervallength").cast("int") * F.lit(60)).alias("aep_sec_per_intrvl"),
        F.lit(None).cast("string").alias("aep_meter_alias"),
        F.col("aep_meter_program"),
        F.lit("interval").cast("string").alias("aep_usage_type"),
        F.lit(timezone_map[opco]).alias("aep_timezone_cd"),
        F.col("endtime").alias("endtimeperiod"),
        F.col("starttime").alias("starttimeperiod"),
        F.when(
            F.col("value_mltplr_flg") == "Y",
            F.col("value").cast("double") * F.col("bill_cnst").cast("double"),
        ).otherwise(F.col("value").cast("double")).alias("value"),
        F.col("aep_raw_value"),
        F.col("bill_cnst").alias("scalarfloat"),
        F.col("aep_acct_cls_cd"),
        F.col("aep_acct_type_cd"),
        F.col("aep_mtr_pnt_nb"),
        F.col("aep_tarf_pnt_nb"),
        F.col("aep_comp_mtr_mltplr").cast("double").alias("aep_comp_mtr_mltplr"),
        F.unix_timestamp(
            F.concat(F.col("endtime"), F.col("interval_epoch")),
            "yyyy-MM-dd'T'HH:mm:ssXXX",
        ).cast("string").alias("aep_endtime_utc"),
        F.col("aep_mtr_removal_ts"),
        F.col("aep_mtr_install_ts"),
        F.col("aep_city"),
        F.col("aep_zip"),
        F.col("aep_state"),
        F.current_timestamp().alias("hdp_insert_dttm"),
        F.current_timestamp().alias("hdp_update_dttm"),
        F.col("hdp_update_user"),
        F.substring(F.col("aep_premise_nb"), 1, 2).cast("string").alias("authority"),
        F.substring(F.col("starttime"), 1, 10).cast("string").alias("aep_usage_dt"),
        F.lit("new").cast("string").alias("data_type"),
        F.col("aep_opco"),
        F.col("aep_meter_bucket"),
    )
    interval_data_files_stg_vw_df.printSchema()

    #######################################################################################################################
    # Reference: iceberg_catalog.stg_nonvee.interval_data_files_{opco}_xfrm
    # Source: scripts/xfrm_interval_data_files.sh (Lines 140-191)
    #         scripts/ddl/stg_nonvee.interval_data_files_{opco}_xfrm.ddl
    #         scripts/ddl/stg_nonvee.interval_data_files_{opco}_xfrm_vw.ddl
    # Input: interval_data_files_{opco}_stg_vw_df (43 columns)
    # Output: interval_data_files_{opco}_xfrm_df (48 columns, deduplicated)
    # Prepare data for Iceberg xfrm table by:
    # - Combined both queries like (stg_nonvee.interval_data_files_{opco}_xfrm.ddl+interval_data_files_{opco}_xfrm_vw.ddl)
    # - Adding 5 NULL columns (toutier, toutiername, aep_billable_ind, aep_data_quality_cd, aep_data_validation)
    # - Deduplication using ROW_NUMBER() partitioned by (serialnumber, endtimeperiod, aep_channel_id, aep_raw_uom)
    #######################################################################################################################
    print(f'===== Step 7: Deduplication + XFRM transformations =====')
    xfrm_df = interval_data_files_stg_vw_df.select(
        F.col("serialnumber"),
        F.col("source"),
        F.col("aep_devicecode"),
        F.col("isvirtual_meter"),
        F.col("timezoneoffset"),
        F.col("aep_premise_nb"),
        F.col("aep_service_point"),
        F.col("aep_mtr_install_ts"),
        F.col("aep_mtr_removal_ts"),
        F.col("aep_srvc_dlvry_id"),
        F.col("aep_comp_mtr_mltplr"),
        F.col("name_register"),
        F.col("isvirtual_register"),
        F.lit(None).cast("string").alias("toutier"),
        F.lit(None).cast("string").alias("toutiername"),
        F.col("aep_srvc_qlty_idntfr"),
        F.col("aep_channel_id"),
        F.col("aep_raw_uom"),
        F.col("aep_sec_per_intrvl").cast("double"),
        F.col("aep_meter_alias"),
        F.col("aep_meter_program"),
        F.lit(None).cast("string").alias("aep_billable_ind"),
        F.col("aep_usage_type"),
        F.col("aep_timezone_cd"),
        F.col("endtimeperiod"),
        F.col("starttimeperiod"),
        F.col("value").cast("float"),
        F.col("aep_raw_value").cast("float"),
        F.col("scalarfloat").cast("float"),
        F.lit(None).cast("string").alias("aep_data_quality_cd"),
        F.lit(None).cast("string").alias("aep_data_validation"),
        F.col("aep_acct_cls_cd"),
        F.col("aep_acct_type_cd"),
        F.col("aep_mtr_pnt_nb"),
        F.col("aep_tarf_pnt_nb"),
        F.col("aep_endtime_utc"),
        F.col("aep_city"),
        F.col("aep_zip"),
        F.col("aep_state"),
        F.col("hdp_update_user"),
        F.lit(current_dttm_ltz).cast("timestamp").alias("hdp_insert_dttm"),
        F.lit(current_dttm_ltz).cast("timestamp").alias("hdp_update_dttm"),
        F.col("authority"),
        F.col("aep_derived_uom"),
        F.col("data_type"),
        F.col("aep_opco"),
        F.col("aep_usage_dt"),
        F.col("aep_meter_bucket")
    )
    xfrm_df.printSchema()

    _time = time.time()
    print(f'===== staging xfrm_df =====')
    xfrm_staging_path = f'{scratch_path}/xfrm_stage/'
    xfrm_df.write.mode("overwrite").partitionBy("aep_usage_dt").parquet(xfrm_staging_path)
    print(f'completed writing in {str(timedelta(seconds=time.time() - _time))}')
    staged_xfrm_df = spark.read.parquet(xfrm_staging_path)
    staged_xfrm_df.printSchema()

    _time = time.time()
    print(f'===== counting staged_xfrm_df records =====')
    staged_xfrm_df_count = staged_xfrm_df.count()
    staged_xfrm_df_partition_count = staged_xfrm_df.rdd.getNumPartitions()
    print(f'counted {staged_xfrm_df_count} record(s) in {staged_xfrm_df_partition_count} partition(s)')
    print(f'completed counting in {str(timedelta(seconds=time.time() - _time))}')

    # Deduplication: stg_nonvee.interval_data_files_{opco}_xfrm_vw.ddl
    print(f'===== dedup staged_xfrm_df =====')
    latest_upd_w = Window.partitionBy(
        "aep_usage_dt", "aep_meter_bucket", "serialnumber", "aep_channel_id", "aep_raw_uom", "endtimeperiod",
    ).orderBy(F.desc("hdp_insert_dttm"))
    deduped_xfrm_df = staged_xfrm_df.select(
        "*", F.row_number().over(latest_upd_w).alias("upd_rownum"),
    ).filter(F.col("upd_rownum") == 1).drop("upd_rownum")

    _time = time.time()
    print(f'===== staging deduped_xfrm_df =====')
    deduped_xfrm_staging_path = f'{scratch_path}/dedup_xfrm_stage/'
    deduped_xfrm_df.write.mode("overwrite").partitionBy("aep_usage_dt").parquet(deduped_xfrm_staging_path)
    print(f'completed writing in {str(timedelta(seconds=time.time() - _time))}')
    staged_deduped_xfrm_df = spark.read.parquet(deduped_xfrm_staging_path)
    staged_deduped_xfrm_df.printSchema()

    _time = time.time()
    print(f'===== counting staged_deduped_xfrm_df records =====')
    staged_deduped_xfrm_df_count = staged_deduped_xfrm_df.count()
    staged_deduped_xfrm_df_partition_count = staged_deduped_xfrm_df.rdd.getNumPartitions()
    print(f'counted {staged_deduped_xfrm_df_count} record(s) in {staged_deduped_xfrm_df_partition_count} partition(s)')
    print(f'completed counting in {str(timedelta(seconds=time.time() - _time))}')

    # ============================================================
    # Step 9: MERGE into Iceberg consume layer
    # Source: dml/usage_nonvee.reading_ivl_nonvee.dml
    # Called by: consume_interval_data_files.py
    # Target: iceberg_catalog.usage_nonvee.reading_ivl_nonvee_{opco}
    # ============================================================
    print(f'===== Step 9: MERGE into consume layer =====')
    # Get distinct usage dates for partition pruning (consume_interval_data_files.py Lines 48-54)
    distinct_usage_dates = staged_deduped_xfrm_df.select("aep_usage_dt").distinct().collect()
    distinct_usage_date_values = [r.aep_usage_dt for r in distinct_usage_dates]
    usage_dates_str = ", ".join([f"'{d}'" for d in distinct_usage_date_values])
    print(f"Distinct usage dates: {usage_dates_str}")

    t_alias = "t"
    s_alias = "s"
    target_table_name = f'usage_nonvee.reading_ivl_nonvee_{opco}'
    source_view_name = f'interval_data_xfrm_{opco}_vw'


    sql_text = dedent(f"""
    MERGE INTO {target_table_name} AS {t_alias}
    USING {source_view_name} AS {s_alias}
       ON {t_alias}.aep_usage_dt in ({usage_dates_str})
      AND {t_alias}.aep_usage_dt = {s_alias}.aep_usage_dt
      AND {t_alias}.aep_meter_bucket = {s_alias}.aep_meter_bucket
      AND {t_alias}.serialnumber = {s_alias}.serialnumber
      AND {t_alias}.aep_channel_id = {s_alias}.aep_channel_id
      AND {t_alias}.aep_raw_uom = {s_alias}.aep_raw_uom
      AND {t_alias}.endtimeperiod = {s_alias}.endtimeperiod
     WHEN MATCHED THEN UPDATE SET
          {t_alias}.aep_srvc_qlty_idntfr = {s_alias}.aep_srvc_qlty_idntfr
        , {t_alias}.aep_channel_id = {s_alias}.aep_channel_id
        , {t_alias}.aep_sec_per_intrvl = {s_alias}.aep_sec_per_intrvl
        , {t_alias}.aep_meter_alias = {s_alias}.aep_meter_alias
        , {t_alias}.aep_meter_program = {s_alias}.aep_meter_program
        , {t_alias}.aep_usage_type = {s_alias}.aep_usage_type
        , {t_alias}.aep_timezone_cd = {s_alias}.aep_timezone_cd
        , {t_alias}.endtimeperiod = {s_alias}.endtimeperiod
        , {t_alias}.starttimeperiod = {s_alias}.starttimeperiod
        , {t_alias}.value = {s_alias}.value
        , {t_alias}.aep_raw_value = {s_alias}.aep_raw_value
        , {t_alias}.scalarfloat = {s_alias}.scalarfloat
        , {t_alias}.aep_acct_cls_cd = {s_alias}.aep_acct_cls_cd
        , {t_alias}.aep_acct_type_cd = {s_alias}.aep_acct_type_cd
        , {t_alias}.aep_mtr_pnt_nb = {s_alias}.aep_mtr_pnt_nb
        , {t_alias}.aep_comp_mtr_mltplr = {s_alias}.aep_comp_mtr_mltplr
        , {t_alias}.aep_endtime_utc = {s_alias}.aep_endtime_utc
        , {t_alias}.aep_mtr_removal_ts = {s_alias}.aep_mtr_removal_ts
        , {t_alias}.aep_mtr_install_ts = {s_alias}.aep_mtr_install_ts
        , {t_alias}.aep_city = {s_alias}.aep_city
        , {t_alias}.aep_zip = {s_alias}.aep_zip
        , {t_alias}.aep_state = {s_alias}.aep_state
        , {t_alias}.hdp_update_user = 'info-update'
        , {t_alias}.hdp_update_dttm = {s_alias}.hdp_update_dttm
        , {t_alias}.authority = {s_alias}.authority
     WHEN NOT MATCHED THEN INSERT (
          serialnumber
        , source
        , aep_devicecode
        , isvirtual_meter
        , timezoneoffset
        , aep_premise_nb
        , aep_service_point
        , aep_srvc_dlvry_id
        , name_register
        , isvirtual_register
        , toutier
        , toutiername
        , aep_derived_uom
        , aep_raw_uom
        , aep_srvc_qlty_idntfr
        , aep_channel_id
        , aep_sec_per_intrvl
        , aep_meter_alias
        , aep_meter_program
        , aep_billable_ind
        , aep_usage_type
        , aep_timezone_cd
        , endtimeperiod
        , starttimeperiod
        , value
        , aep_raw_value
        , scalarfloat
        , aep_data_quality_cd
        , aep_data_validation
        , aep_acct_cls_cd
        , aep_acct_type_cd
        , aep_mtr_pnt_nb
        , aep_tarf_pnt_nb
        , aep_comp_mtr_mltplr
        , aep_endtime_utc
        , aep_mtr_removal_ts
        , aep_mtr_install_ts
        , aep_city
        , aep_zip
        , aep_state
        , hdp_update_user
        , hdp_insert_dttm
        , hdp_update_dttm
        , authority
        , aep_opco
        , aep_usage_dt
        , aep_meter_bucket
        ) VALUES (
          {s_alias}.serialnumber
        , {s_alias}.source
        , {s_alias}.aep_devicecode
        , {s_alias}.isvirtual_meter
        , {s_alias}.timezoneoffset
        , {s_alias}.aep_premise_nb
        , {s_alias}.aep_service_point
        , {s_alias}.aep_srvc_dlvry_id
        , {s_alias}.name_register
        , {s_alias}.isvirtual_register
        , {s_alias}.toutier
        , {s_alias}.toutiername
        , {s_alias}.aep_derived_uom
        , {s_alias}.aep_raw_uom
        , {s_alias}.aep_srvc_qlty_idntfr
        , {s_alias}.aep_channel_id
        , {s_alias}.aep_sec_per_intrvl
        , {s_alias}.aep_meter_alias
        , {s_alias}.aep_meter_program
        , {s_alias}.aep_billable_ind
        , {s_alias}.aep_usage_type
        , {s_alias}.aep_timezone_cd
        , {s_alias}.endtimeperiod
        , {s_alias}.starttimeperiod
        , {s_alias}.value
        , {s_alias}.aep_raw_value
        , {s_alias}.scalarfloat
        , {s_alias}.aep_data_quality_cd
        , {s_alias}.aep_data_validation
        , {s_alias}.aep_acct_cls_cd
        , {s_alias}.aep_acct_type_cd
        , {s_alias}.aep_mtr_pnt_nb
        , {s_alias}.aep_tarf_pnt_nb
        , {s_alias}.aep_comp_mtr_mltplr
        , {s_alias}.aep_endtime_utc
        , {s_alias}.aep_mtr_removal_ts
        , {s_alias}.aep_mtr_install_ts
        , {s_alias}.aep_city
        , {s_alias}.aep_zip
        , {s_alias}.aep_state
        , 'info-insert'
        , {s_alias}.hdp_insert_dttm
        , {s_alias}.hdp_update_dttm
        , {s_alias}.authority
        , {s_alias}.aep_opco
        , {s_alias}.aep_usage_dt
        , {s_alias}.aep_meter_bucket
        )
    """)
    _time = time.time()
    print(f'===== writing with =====')
    print(sql_text)
    staged_deduped_xfrm_df.createOrReplaceTempView(source_view_name)
    spark.sql(sql_text)
    print(f'completed writing in {str(timedelta(seconds=time.time() - _time))}')

    # ============================================================
    # Step 10: DELETE old + INSERT into downstream incremental table
    # Source: xfrm_interval_data_to_downstream.py + insert_reading_ivl_nonvee_incr_info.dml
    # Target: iceberg_catalog.xfrm_interval.reading_ivl_nonvee_incr
    # - Delete data older than 8 days based on run_date
    # - INSERT from xfrm view with WHERE data_type='new' and aep_opco='{opco}'
    # - Partitioned by (aep_opco, run_date)
    # ============================================================
    target_incr_table = "xfrm_interval.reading_ivl_nonvee_incr"
    print(f'===== Step 10: DELETE old + INSERT into downstream table {target_incr_table} =====')

    # Step 10a: DELETE old data (older than 8 days)
    delete_old_data(spark, target_incr_table, opco)

    # Step 10b: INSERT new data

    reading_ivl_nonvee_incr_df = xfrm_df.filter(
        (F.col("data_type") == "new") & (F.col("aep_opco") == opco)
    ).select(
        "serialnumber",
        "source",
        "aep_devicecode",
        "isvirtual_meter",
        "timezoneoffset",
        "aep_premise_nb",
        "aep_service_point",
        "aep_mtr_install_ts",
        "aep_mtr_removal_ts",
        "aep_srvc_dlvry_id",
        "aep_comp_mtr_mltplr",
        "name_register",
        "isvirtual_register",
        "toutier",
        "toutiername",
        "aep_srvc_qlty_idntfr",
        "aep_channel_id",
        "aep_raw_uom",
        "aep_sec_per_intrvl",
        "aep_meter_alias",
        "aep_meter_program",
        "aep_billable_ind",
        "aep_usage_type",
        "aep_timezone_cd",
        "endtimeperiod",
        "starttimeperiod",
        "value",
        "aep_raw_value",
        "scalarfloat",
        "aep_data_quality_cd",
        "aep_data_validation",
        "aep_acct_cls_cd",
        "aep_acct_type_cd",
        "aep_mtr_pnt_nb",
        "aep_tarf_pnt_nb",
        "aep_endtime_utc",
        "aep_city",
        "aep_zip",
        "aep_state",
        F.lit("info-insert").cast("string").alias("hdp_update_user"),
        F.col("hdp_insert_dttm").cast("timestamp").alias("hdp_insert_dttm"),
        F.col("hdp_update_dttm").cast("timestamp").alias("hdp_update_dttm"),
        "authority",
        "aep_usage_dt",
        "aep_derived_uom",
        "aep_opco",
        F.lit(current_dttm_ltz.strftime("%Y%m%d_%H%M%S")).alias("run_date"),
    )

    _time = time.time()
    print(f'===== writing to {target_incr_table} with append() =====')
    reading_ivl_nonvee_incr_df.writeTo(target_incr_table).append()
    print(f'completed writing in {str(timedelta(seconds=time.time() - _time))}')
    print(f"===== Data is successfully appended on table: {target_incr_table} =====")

    # delete scratch directory (includes raw_staging + dedup_xfrm_stage)
    hadoop_conf = spark._jsc.hadoopConfiguration()
    fs = spark._jvm.org.apache.hadoop.fs.FileSystem.get(hadoop_conf)
    path = spark._jvm.org.apache.hadoop.fs.Path(scratch_path)
    if fs.exists(path):
        print(f"Deleting HDFS path: {path}")
        fs.delete(path, True)

    spark.stop()
    print(f'completed job in {str(timedelta(seconds=time.time() - _bgn_time))}')

if __name__ == "__main__":
    main()
