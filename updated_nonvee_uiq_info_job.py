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
  nonvee_uiq_info_job.py \
  --job_name uiq-nonvee-info \
  --aws_env dev \
  --opco oh \
  --work_bucket aep-datalake-work-dev \
  --archive_bucket aep-datalake-raw-dev \
  --batch_start_dttm_str "2024-10-25 00:00:00" \
  --batch_end_dttm_str "2024-10-26 00:00:00" \
  --skip_archive true
"""

"""
spark-submit --deploy-mode client --packages com.databricks:spark-xml_2.12:0.18.0 --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions --conf spark.sql.catalog.spark_catalog=org.apache.iceberg.spark.SparkSessionCatalog --conf spark.sql.catalog.spark_catalog.type=glue --conf spark.sql.catalog.spark_catalog.glue.id=889415020100 --conf spark.sql.catalog.spark_catalog.warehouse=s3://aep-datalake-work-dev/test/warehouse --conf spark.sql.catalog.iceberg_catalog=org.apache.iceberg.spark.SparkCatalog --conf spark.sql.catalog.iceberg_catalog.type=glue --conf spark.sql.catalog.iceberg_catalog.glue.id=889415020100 --conf spark.sql.catalog.iceberg_catalog.warehouse=s3://aep-datalake-work-dev/test/warehouse --conf spark.sql.session.timeZone="America/New_York" --conf spark.driver.extraJavaOptions="-Duser.timezone=America/New_York" --conf spark.executor.extraJavaOptions="-Duser.timezone=America/New_York" --conf spark.sql.legacy.timeParserPolicy=LEGACY --driver-cores 2 --driver-memory 10g --conf spark.driver.maxResultSize=5g --executor-cores 1 --executor-memory 5g nonvee_uiq_info_job.py --job_name uiq-nonvee-info --aws_env dev --opco oh --work_bucket aep-datalake-work-dev --archive_bucket aep-datalake-raw-dev --batch_start_dttm_str "2024-10-25 00:00:00" --batch_end_dttm_str "2024-10-26 00:00:00" --skip_archive true
"""

import time
import json
import uuid
import boto3
import argparse

from typing import Dict, List
from textwrap import dedent
from functools import reduce
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pyspark.sql import SparkSession, DataFrame
import pyspark.sql.functions as f
from pyspark.sql.types import StructType, StructField, StringType, ArrayType
from pyspark.sql.window import Window
from pyspark.sql.utils import AnalysisException

data_prefix = "raw/intervals/nonvee/uiq_info"
marker_file_name = "index.txt"
marker_file_max_depth = 2
data_file_ext = ".xml"
data_file_min_size = 0

xml_row_tag = "MeterData"
xml_read_mode = "FAILFAST"
xml_infer_schema = "false"
xml_tag_ignore_namespace = "true"
xml_data_timezone = "America/New_York"
xml_tag_value_col_name = "tag_value"

# OPCO to Company Code mapping (for MACS filter)
OPCO_TO_CO_CD = {
    'oh':  ['07', '10'],
    'im':  ['04'],
    'pso': ['95'],
    'tx':  ['94', '97'],
    'ap':  ['01', '02', '06'],
    'swp': ['96']
}

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


######################################################################
# Reference: stg_nonvee.interval_data_files_{opco}_src_vw
# Source: scripts/ddl/stg_nonvee.interval_data_files_{opco}_src_vw.ddl
# Called by: scripts/source_interval_data_files.sh
# UOM Mapping Reference Table
######################################################################
def get_uom_mapping_table(
    spark: SparkSession,
    opco: str,
    table_name: str = "usage_nonvee.uom_mapping",
) -> "DataFrame":
    """
    Extracts uom_mapping data from the `usage_nonvee` layer
    and filters for specific OPCO data.
    """
    print(f"===== Reading table {table_name} =====")
    uom_mapping_df = spark.table(table_name).filter(
        f.col("aep_opco") == opco
    ).select(
        f.col("aep_channel_id"),
        f.col("aep_derived_uom"),
        f.col("name_register"),
        f.col("aep_srvc_qlty_idntfr"),
        f.col("value_mltplr_flg"),
    )
    print("===== Table read successfully =====")
    return uom_mapping_df


######################################################################
# Reference: stage_interval_xml_files.sh
# The columns are referenced from the script for reading purposes.
######################################################################
def get_macs_ami_table(
    spark: SparkSession,
    target_co_cd_ownr: List[str] = None,
    table_name: str = "stg_nonvee.meter_premise_macs_ami",
) -> DataFrame:
    """
    Extracts meter_premise_macs_ami data from the `stg_nonvee` layer
    and filters for the specified OPCO company codes.
    """
    print(f"===== Reading table {table_name} =====")
    ami_df = spark.table(table_name).filter(
        f.col("co_cd_ownr").isin(target_co_cd_ownr)
    ).select(
        f.col("prem_nb"),
        f.col("bill_cnst"),
        f.col("acct_clas_cd"),
        f.col("acct_type_cd"),
        f.col("devc_cd"),
        f.col("pgm_id_nm"),
        f.concat(
            f.coalesce(f.col("tx_mand_data"), f.lit("")),
            f.coalesce(f.col("doe_nb"), f.lit("")),
            f.coalesce(f.col("serv_deliv_id"), f.lit("")),
        ).cast("string").alias("sd"),
        f.col("mfr_devc_ser_nbr"),
        f.col("mtr_inst_ts"),
        f.col("mtr_rmvl_ts"),
        f.col("acct_turn_on_dt"),
        f.col("acct_turn_off_dt"),
        f.col("bill_acct_nb"),
        f.col("mtr_pnt_nb"),
        f.col("tarf_pnt_nb"),
        f.lit(None).cast("string").alias("cmsg_mtr_mult_nb"),
        f.unix_timestamp(
            "mtr_inst_ts", "yyyy-MM-dd HH:mm:ss"
        ).alias("unix_mtr_inst_ts"),
        f.when(
            f.col("mtr_rmvl_ts") == "9999-01-01",
            f.unix_timestamp("mtr_rmvl_ts", "yyyy-MM-dd")
        ).otherwise(
            f.unix_timestamp("mtr_rmvl_ts", "yyyy-MM-dd HH:mm:ss")
        ).alias("unix_mtr_rmvl_ts"),
        f.unix_timestamp(
            "acct_turn_on_dt", "yyyy-MM-dd"
        ).alias("unix_acct_turn_on_dt"),
        f.unix_timestamp(
            "acct_turn_off_dt", "yyyy-MM-dd"
        ).alias("unix_acct_turn_off_dt"),
        f.col("serv_city_ad").alias("aep_city"),
        f.col("serv_zip_ad").alias("aep_zip"),
        f.col("state_cd").alias("aep_state"),
    )
    print("===== Table read successfully =====")
    return ami_df


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
    aws_env = args.aws_env
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
    df_raw.printSchema()
    print(f'completed reading in {str(timedelta(seconds=time.time() - _time))}')

    _time = time.time()
    print(f'===== staging raw data =====')
    raw_staging_path = f'{scratch_path}/raw_stage/'
    df_raw.coalesce(1000).write.mode("overwrite").parquet(raw_staging_path)
    print(f'completed staging in {str(timedelta(seconds=time.time() - _time))}')
    staged_raw_df = spark.read.parquet(raw_staging_path)

    _time = time.time()
    print(f'===== counting staged_df records =====')
    staged_raw_df_count = staged_raw_df.count()
    staged_raw_df_partition_count = staged_raw_df.rdd.getNumPartitions()
    print(f'counted {staged_raw_df_count} record(s) in {staged_raw_df_partition_count} partition(s)')
    print(f'completed counting in {str(timedelta(seconds=time.time() - _time))}')

    part_date = batch_end_dttm_ltz.strftime("%Y%m%d_%H%M")

    # read macs_ami and uom_mapping tables
    macs_df = get_macs_ami_table(spark, target_co_cd_ownr=OPCO_TO_CO_CD[opco])
    uom_mapping_df = get_uom_mapping_table(spark, opco)

    _time = time.time()
    print(f'===== Step 1: Rename columns =====')
    #rename columns
    interval_data_files_src_df = (
        staged_raw_df
        .withColumnRenamed("_MeterName", "metername")
        .withColumnRenamed("_UtilDeviceID", "utildeviceid")
        .withColumnRenamed("_MacID", "macid")
        .withColumnRenamed("IntervalReadData", "interval_reading")
        .withColumn("part_date", f.lit(part_date))
    )
    print(f'completed Step 1 in {str(timedelta(seconds=time.time() - _time))}')

    ###############################################################################
    # Reference: stg_nonvee.interval_data_files_{opco}_src_vw
    # Source: scripts/ddl/stg_nonvee.interval_data_files_{opco}_src_vw.ddl
    # Called by: scripts/source_interval_data_files.sh
    # 1st EXPLODE: Flatten interval_reading array
    ###############################################################################
    _time = time.time()
    print(f'===== Step 2: 1st EXPLODE (intervals) =====')
    interval_exploded_df = (
        interval_data_files_src_df
        .withColumn("exp_interval", f.explode("interval_reading.Interval"))
        .select(
            f.col("metername").alias("MeterName"),
            f.col("utildeviceid").alias("UtilDeviceID"),
            f.col("macid").alias("MacID"),
            f.col("interval_reading._IntervalLength").alias("IntervalLength"),
            f.col("interval_reading._StartTime").alias("blockstarttime"),
            f.col("interval_reading._EndTime").alias("blockendtime"),
            f.col("interval_reading._NumberIntervals").alias("NumberIntervals"),
            f.col("part_date"),
            f.col("exp_interval._EndTime").alias("int_endtime"),
            f.col("exp_interval._GatewayCollectedTime").alias("int_gatewaycollectedtime"),
            f.col("exp_interval._BlockSequenceNumber").alias("int_blocksequencenumber"),
            f.col("exp_interval._IntervalSequenceNumber").alias("int_intervalsequencenumber"),
            f.col("exp_interval.Reading").alias("int_reading"),
        )
        .filter(
            f.date_format(
                f.to_timestamp(f.substring("blockstarttime", 1, 10), "yyyy-MM-dd"),
                "yyyyMMdd",
            ).cast("int") >= 20150301
        )
    )
    print(f'completed Step 2 in {str(timedelta(seconds=time.time() - _time))}')

    ###############################################################################
    # 2nd EXPLODE: Flatten int_reading + time calculations
    ###############################################################################
    _time = time.time()
    print(f'===== Step 3: 2nd EXPLODE (readings) =====')
    reading_exploded_df = (
        interval_exploded_df
        .withColumn("exp_reading", f.explode("int_reading"))
        .select(
            f.col("MeterName"),
            f.col("UtilDeviceID"),
            f.col("MacID"),
            f.col("IntervalLength"),
            f.col("blockstarttime"),
            f.col("blockendtime"),
            f.col("NumberIntervals"),
            f.from_unixtime(
                f.unix_timestamp(
                    f.substring("int_endtime", 1, 19), "yyyy-MM-dd'T'HH:mm:ss"
                ) - (f.col("IntervalLength").cast("int") * 60),
                "yyyy-MM-dd'T'HH:mm:ss",
            ).alias("starttime"),
            f.from_unixtime(
                f.unix_timestamp(
                    f.substring("int_endtime", 1, 19), "yyyy-MM-dd'T'HH:mm:ss"
                ),
                "yyyy-MM-dd'T'HH:mm:ss",
            ).alias("endtime"),
            f.substring("int_endtime", -6, 6).alias("interval_epoch"),
            f.col("int_gatewaycollectedtime"),
            f.col("int_blocksequencenumber"),
            f.col("int_intervalsequencenumber"),
            f.col("exp_reading._Channel").alias("channel"),
            f.col("exp_reading._RawValue").alias("rawvalue"),
            f.col("exp_reading._Value").alias("value"),
            f.col("exp_reading._UOM").alias("uom"),
            f.col("part_date"),
        )
        .filter(
            f.col("channel").isNotNull()
            & f.col("rawvalue").isNotNull()
            & f.col("value").isNotNull()
            & f.col("uom").isNotNull()
        )
    )
    print(f'completed Step 3 in {str(timedelta(seconds=time.time() - _time))}')

    ###############################################################################
    # Reference: stg_nonvee.interval_data_files_oh_src_vw.ddl
    # LEFT JOIN with uom_mapping on reading.channel = um.aep_channel_id
    ###############################################################################
    _time = time.time()
    print(f'===== Step 4: LEFT JOIN with UOM mapping =====')
    interval_data_files_src_vw_df = (
        reading_exploded_df.alias("reading")
        .join(
            uom_mapping_df.alias("um"),
            on=(f.col("reading.channel") == f.col("um.aep_channel_id")),
            how="left",
        )
        .select(
            f.trim(f.col("reading.MeterName")).alias("MeterName"),
            f.col("reading.UtilDeviceID"),
            f.col("reading.MacID"),
            f.col("reading.IntervalLength"),
            f.col("reading.blockstarttime"),
            f.col("reading.blockendtime"),
            f.col("reading.NumberIntervals"),
            f.concat(
                f.col("reading.starttime"), f.col("reading.interval_epoch"),
            ).alias("starttime"),
            f.concat(
                f.col("reading.endtime"), f.col("reading.interval_epoch"),
            ).alias("endtime"),
            f.col("reading.interval_epoch"),
            f.col("reading.int_gatewaycollectedtime"),
            f.col("reading.int_blocksequencenumber"),
            f.col("reading.int_intervalsequencenumber"),
            f.col("reading.channel"),
            f.col("reading.rawvalue"),
            f.col("reading.value"),
            f.lower(f.trim(f.col("reading.uom"))).alias("uom"),
            f.col("reading.part_date"),
            f.coalesce(f.col("um.aep_derived_uom"), f.lit("UNK")).alias("aep_uom"),
            f.when(
                f.col("um.name_register").isNotNull(),
                f.translate(
                    f.col("um.name_register"), "mtr_ivl_len", f.col("reading.IntervalLength"),
                ),
            ).otherwise(f.lit("UNK")).cast("string").alias("name_register"),
            f.coalesce(f.col("um.aep_srvc_qlty_idntfr"), f.lit("UNK")).alias("aep_sqi"),
            f.col("um.value_mltplr_flg"),
            f.unix_timestamp(
                "reading.endtime", "yyyy-MM-dd'T'HH:mm:ssXXX",
            ).alias("unix_endtime"),
        )
    )
    print(f'completed Step 4 in {str(timedelta(seconds=time.time() - _time))}')

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
    _time = time.time()
    print(f'===== Step 5: LEFT JOIN with MACS =====')
    interval_data_files_stg_df = (
        interval_data_files_src_vw_df.alias("src")
        .join(
            macs_df.alias("macs"),
            on=(
                (f.col("src.MeterName") == f.col("macs.mfr_devc_ser_nbr"))
                & (f.col("src.unix_endtime").between(
                    f.col("macs.unix_mtr_inst_ts"), f.col("macs.unix_mtr_rmvl_ts"),
                ))
                & (f.col("src.unix_endtime").between(
                    f.col("macs.unix_acct_turn_on_dt"), f.col("macs.unix_acct_turn_off_dt"),
                ))
            ),
            how="left",
        )
        .select(
            f.lit(opco).alias("aep_opco"),
            f.col("src.MeterName").alias("serialnumber"),
            f.col("src.UtilDeviceID").alias("utildeviceid"),
            f.col("src.MacID").alias("macid"),
            f.col("src.IntervalLength").alias("intervallength"),
            f.col("src.blockstarttime"),
            f.col("src.blockendtime"),
            f.col("src.NumberIntervals").alias("numberintervals"),
            f.col("src.starttime"),
            f.col("src.endtime"),
            f.col("src.interval_epoch"),
            f.col("src.int_gatewaycollectedtime"),
            f.col("src.int_blocksequencenumber"),
            f.col("src.int_intervalsequencenumber"),
            f.col("src.channel"),
            f.col("src.value").alias("aep_raw_value"),
            f.col("src.value"),
            f.col("src.uom").alias("aep_raw_uom"),
            f.upper(f.col("src.aep_uom")).alias("aep_derived_uom"),
            f.col("src.name_register"),
            f.col("src.aep_sqi").alias("aep_srvc_qlty_idntfr"),
            f.col("macs.prem_nb").alias("aep_premise_nb"),
            f.col("macs.bill_cnst"),
            f.col("macs.acct_clas_cd").alias("aep_acct_cls_cd"),
            f.col("macs.acct_type_cd").alias("aep_acct_type_cd"),
            f.col("macs.devc_cd").alias("aep_devicecode"),
            f.col("macs.pgm_id_nm").alias("aep_meter_program"),
            f.col("macs.sd").alias("aep_srvc_dlvry_id"),
            f.col("macs.mtr_pnt_nb").alias("aep_mtr_pnt_nb"),
            f.col("macs.tarf_pnt_nb").alias("aep_tarf_pnt_nb"),
            f.col("macs.cmsg_mtr_mult_nb").alias("aep_comp_mtr_mltplr"),
            f.col("macs.mtr_rmvl_ts").alias("aep_mtr_removal_ts"),
            f.col("macs.mtr_inst_ts").alias("aep_mtr_install_ts"),
            f.col("macs.aep_city"),
            f.substring(f.col("macs.aep_zip"), 1, 5).cast("string").alias("aep_zip"),
            f.col("macs.aep_state"),
            f.lit("info-insert").cast("string").alias("hdp_update_user"),
            f.col("src.part_date"),
            f.col("src.value_mltplr_flg"),
            f.substring(
                f.trim(f.col("src.MeterName")), -2, 2,
            ).cast("string").alias("aep_meter_bucket"),
        )
    )
    print(f'completed Step 5 in {str(timedelta(seconds=time.time() - _time))}')

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
    _time = time.time()
    print(f'===== Step 6: STG_VW transformations =====')
    interval_data_files_stg_vw_df = interval_data_files_stg_df.select(
        f.col("serialnumber"),
        f.lit("nonvee-hes").cast("string").alias("source"),
        f.col("aep_devicecode"),
        f.lit("N").cast("string").alias("isvirtual_meter"),
        f.col("interval_epoch").alias("timezoneoffset"),
        f.col("aep_premise_nb"),
        f.when(
            f.col("aep_premise_nb").isNotNull()
            & f.col("aep_tarf_pnt_nb").isNotNull()
            & f.col("aep_mtr_pnt_nb").isNotNull(),
            f.concat(
                f.col("aep_premise_nb"),
                f.lit("-"),
                f.col("aep_tarf_pnt_nb"),
                f.lit("-"),
                f.col("aep_mtr_pnt_nb"),
            ),
        ).otherwise(f.lit(None)).cast("string").alias("aep_service_point"),
        f.col("aep_srvc_dlvry_id"),
        f.col("name_register"),
        f.lit("N").cast("string").alias("isvirtual_register"),
        f.col("aep_derived_uom"),
        f.col("aep_raw_uom"),
        f.col("aep_srvc_qlty_idntfr"),
        f.col("channel").alias("aep_channel_id"),
        (f.col("intervallength").cast("int") * 60).alias("aep_sec_per_intrvl"),
        f.lit(None).cast("string").alias("aep_meter_alias"),
        f.col("aep_meter_program"),
        f.lit("interval").cast("string").alias("aep_usage_type"),
        f.lit("US/Eastern").cast("string").alias("aep_timezone_cd"),
        f.col("endtime").alias("endtimeperiod"),
        f.col("starttime").alias("starttimeperiod"),
        f.when(
            f.col("value_mltplr_flg") == "Y",
            f.col("value").cast("double") * f.col("bill_cnst").cast("double"),
        ).otherwise(f.col("value").cast("double")).alias("value"),
        f.col("aep_raw_value"),
        f.col("bill_cnst").alias("scalarfloat"),
        f.col("aep_acct_cls_cd"),
        f.col("aep_acct_type_cd"),
        f.col("aep_mtr_pnt_nb"),
        f.col("aep_tarf_pnt_nb"),
        f.col("aep_comp_mtr_mltplr").cast("double").alias("aep_comp_mtr_mltplr"),
        f.unix_timestamp(
            f.concat(f.col("endtime"), f.col("interval_epoch")),
            "yyyy-MM-dd'T'HH:mm:ssXXX",
        ).cast("string").alias("aep_endtime_utc"),
        f.col("aep_mtr_removal_ts"),
        f.col("aep_mtr_install_ts"),
        f.col("aep_city"),
        f.col("aep_zip"),
        f.col("aep_state"),
        f.current_timestamp().alias("hdp_insert_dttm"),
        f.current_timestamp().alias("hdp_update_dttm"),
        f.col("hdp_update_user"),
        f.substring(f.col("aep_premise_nb"), 1, 2).cast("string").alias("authority"),
        f.substring(f.col("starttime"), 1, 10).cast("string").alias("aep_usage_dt"),
        f.lit("new").cast("string").alias("data_type"),
        f.col("aep_opco"),
        f.col("aep_meter_bucket"),
    )
    print(f'completed Step 6 in {str(timedelta(seconds=time.time() - _time))}')

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
    _time = time.time()
    xfrm_df = interval_data_files_stg_vw_df.select(
        f.col("serialnumber"),
        f.col("source"),
        f.col("aep_devicecode"),
        f.col("isvirtual_meter"),
        f.col("timezoneoffset"),
        f.col("aep_premise_nb"),
        f.col("aep_service_point"),
        f.col("aep_mtr_install_ts"),
        f.col("aep_mtr_removal_ts"),
        f.col("aep_srvc_dlvry_id"),
        f.col("aep_comp_mtr_mltplr"),
        f.col("name_register"),
        f.col("isvirtual_register"),
        f.lit(None).cast("string").alias("toutier"),
        f.lit(None).cast("string").alias("toutiername"),
        f.col("aep_srvc_qlty_idntfr"),
        f.col("aep_channel_id"),
        f.col("aep_raw_uom"),
        f.col("aep_sec_per_intrvl").cast("double"),
        f.col("aep_meter_alias"),
        f.col("aep_meter_program"),
        f.lit(None).cast("string").alias("aep_billable_ind"),
        f.col("aep_usage_type"),
        f.col("aep_timezone_cd"),
        f.col("endtimeperiod"),
        f.col("starttimeperiod"),
        f.col("value").cast("float"),
        f.col("aep_raw_value").cast("float"),
        f.col("scalarfloat").cast("float"),
        f.lit(None).cast("string").alias("aep_data_quality_cd"),
        f.lit(None).cast("string").alias("aep_data_validation"),
        f.col("aep_acct_cls_cd"),
        f.col("aep_acct_type_cd"),
        f.col("aep_mtr_pnt_nb"),
        f.col("aep_tarf_pnt_nb"),
        f.col("aep_endtime_utc"),
        f.col("aep_city"),
        f.col("aep_zip"),
        f.col("aep_state"),
        f.col("hdp_update_user"),
        f.col("hdp_insert_dttm").cast("timestamp"),
        f.col("hdp_update_dttm").cast("timestamp"),
        f.col("authority"),
        f.col("aep_derived_uom"),
        f.col("data_type"),
        f.col("aep_opco"),
        f.col("aep_usage_dt"),
        f.col("aep_meter_bucket")
    )

    # Deduplication: stg_nonvee.interval_data_files_{opco}_xfrm_vw.ddl
    latest_upd_w = Window.partitionBy(
        "serialnumber",
        "endtimeperiod",
        "aep_channel_id",
        "aep_raw_uom",
    ).orderBy(f.col("hdp_insert_dttm").desc())
    deduped_xfrm_df = xfrm_df.select(
        "*", f.row_number().over(latest_upd_w).alias("upd_rownum"),
    ).filter(f.col("upd_rownum") == 1).drop("upd_rownum")
    print(f'completed Step 7 in {str(timedelta(seconds=time.time() - _time))}')

    # Step 8: Checkpoint + create temp view for MERGE
    print(f'===== Step 8: Checkpoint + Temp View =====')
    _time = time.time()
    spark.sparkContext.setCheckpointDir(f"s3://{work_bucket}/temp/checkpoint/")
    mat_xfrm_vw_df = deduped_xfrm_df.checkpoint()
    mat_xfrm_vw_df.createOrReplaceTempView("interval_data_files_xfrm_vw")

    # Get distinct usage dates for partition pruning (consume_interval_data_files.py Lines 48-54)
    usage_dates_list = [row.aep_usage_dt for row in mat_xfrm_vw_df.select("aep_usage_dt").distinct().collect()]
    usage_dates_str = ",".join([f"'{d}'" for d in usage_dates_list])
    print(f'completed Step 8 in {str(timedelta(seconds=time.time() - _time))}')

    # ============================================================
    # Step 9: MERGE into Iceberg consume layer
    # Source: dml/usage_nonvee.reading_ivl_nonvee.dml
    # Called by: consume_interval_data_files.py
    # Target: iceberg_catalog.usage_nonvee.reading_ivl_nonvee_{opco}
    # ============================================================
    print(f'===== Step 9: MERGE into consume layer =====')
    _time = time.time()

    t = "t"
    s = "s"
    target_table = f'iceberg_catalog.usage_nonvee.reading_ivl_nonvee_{opco}'
    source_view = "interval_data_files_xfrm_vw"
    merge_sql = dedent(f"""
    MERGE INTO {target_table} AS {t}
    USING (
        SELECT
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
            , to_timestamp(hdp_insert_dttm, 'yyyy-MM-dd HH:mm:ss.SSSSSS') as hdp_insert_dttm
            , to_timestamp(hdp_insert_dttm, 'yyyy-MM-dd HH:mm:ss.SSSSSS') as hdp_update_dttm
            , hdp_update_user
            , authority
            , aep_opco
            , aep_usage_dt
            , aep_meter_bucket
        FROM {source_view}
    ) AS {s}
       ON {t}.aep_usage_dt IN ({usage_dates_str})
      AND {t}.aep_usage_dt = {s}.aep_usage_dt
      AND {t}.aep_meter_bucket = {s}.aep_meter_bucket
      AND {t}.serialnumber = {s}.serialnumber
      AND {t}.aep_channel_id = {s}.aep_channel_id
      AND {t}.aep_raw_uom = {s}.aep_raw_uom
      AND {t}.endtimeperiod = {s}.endtimeperiod
     WHEN MATCHED THEN UPDATE SET
          {t}.aep_srvc_qlty_idntfr = {s}.aep_srvc_qlty_idntfr
        , {t}.aep_channel_id = {s}.aep_channel_id
        , {t}.aep_sec_per_intrvl = {s}.aep_sec_per_intrvl
        , {t}.aep_meter_alias = {s}.aep_meter_alias
        , {t}.aep_meter_program = {s}.aep_meter_program
        , {t}.aep_usage_type = {s}.aep_usage_type
        , {t}.aep_timezone_cd = {s}.aep_timezone_cd
        , {t}.endtimeperiod = {s}.endtimeperiod
        , {t}.starttimeperiod = {s}.starttimeperiod
        , {t}.value = {s}.value
        , {t}.aep_raw_value = {s}.aep_raw_value
        , {t}.scalarfloat = {s}.scalarfloat
        , {t}.aep_acct_cls_cd = {s}.aep_acct_cls_cd
        , {t}.aep_acct_type_cd = {s}.aep_acct_type_cd
        , {t}.aep_mtr_pnt_nb = {s}.aep_mtr_pnt_nb
        , {t}.aep_comp_mtr_mltplr = {s}.aep_comp_mtr_mltplr
        , {t}.aep_endtime_utc = {s}.aep_endtime_utc
        , {t}.aep_mtr_removal_ts = {s}.aep_mtr_removal_ts
        , {t}.aep_mtr_install_ts = {s}.aep_mtr_install_ts
        , {t}.aep_city = {s}.aep_city
        , {t}.aep_zip = {s}.aep_zip
        , {t}.aep_state = {s}.aep_state
        , {t}.hdp_update_user = 'info-update'
        , {t}.hdp_update_dttm = {s}.hdp_update_dttm
        , {t}.authority = {s}.authority
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
          {s}.serialnumber
        , {s}.source
        , {s}.aep_devicecode
        , {s}.isvirtual_meter
        , {s}.timezoneoffset
        , {s}.aep_premise_nb
        , {s}.aep_service_point
        , {s}.aep_srvc_dlvry_id
        , {s}.name_register
        , {s}.isvirtual_register
        , {s}.toutier
        , {s}.toutiername
        , {s}.aep_derived_uom
        , {s}.aep_raw_uom
        , {s}.aep_srvc_qlty_idntfr
        , {s}.aep_channel_id
        , {s}.aep_sec_per_intrvl
        , {s}.aep_meter_alias
        , {s}.aep_meter_program
        , {s}.aep_billable_ind
        , {s}.aep_usage_type
        , {s}.aep_timezone_cd
        , {s}.endtimeperiod
        , {s}.starttimeperiod
        , {s}.value
        , {s}.aep_raw_value
        , {s}.scalarfloat
        , {s}.aep_data_quality_cd
        , {s}.aep_data_validation
        , {s}.aep_acct_cls_cd
        , {s}.aep_acct_type_cd
        , {s}.aep_mtr_pnt_nb
        , {s}.aep_tarf_pnt_nb
        , {s}.aep_comp_mtr_mltplr
        , {s}.aep_endtime_utc
        , {s}.aep_mtr_removal_ts
        , {s}.aep_mtr_install_ts
        , {s}.aep_city
        , {s}.aep_zip
        , {s}.aep_state
        , 'info-insert'
        , {s}.hdp_insert_dttm
        , {s}.hdp_update_dttm
        , {s}.authority
        , {s}.aep_opco
        , {s}.aep_usage_dt
        , {s}.aep_meter_bucket
        )
    """)
    print(merge_sql)
    spark.sql(merge_sql)
    print(f'completed Step 9 MERGE in {str(timedelta(seconds=time.time() - _time))}')

    # ============================================================
    # Step 10: DELETE old + INSERT into downstream incremental table
    # Source: xfrm_interval_data_to_downstream.py + insert_reading_ivl_nonvee_incr_info.dml
    # Target: iceberg_catalog.xfrm_interval.reading_ivl_nonvee_incr
    # - Delete data older than 8 days based on run_date
    # - INSERT from xfrm view with WHERE data_type='new' and aep_opco='{opco}'
    # - Partitioned by (aep_opco, run_date)
    # ============================================================
    print(f'===== Step 10: DELETE old + INSERT into downstream table =====')
    _time = time.time()

    target_incr_table = "iceberg_catalog.xfrm_interval.reading_ivl_nonvee_incr"

    # Step 10a: DELETE old data (older than 8 days)
    delete_old_data(spark, target_incr_table, opco)

    # Step 10b: INSERT new data
    insert_downstream_sql = dedent(f"""
    INSERT INTO {target_incr_table}
    SELECT
          serialnumber
        , source
        , aep_devicecode
        , isvirtual_meter
        , timezoneoffset
        , aep_premise_nb
        , aep_service_point
        , aep_mtr_install_ts
        , aep_mtr_removal_ts
        , aep_srvc_dlvry_id
        , aep_comp_mtr_mltplr
        , name_register
        , isvirtual_register
        , toutier
        , toutiername
        , aep_srvc_qlty_idntfr
        , aep_channel_id
        , aep_raw_uom
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
        , aep_endtime_utc
        , aep_city
        , aep_zip
        , aep_state
        , hdp_update_user
        , hdp_insert_dttm
        , hdp_update_dttm
        , authority
        , aep_usage_dt
        , aep_derived_uom
        , aep_opco
        , date_format(current_timestamp, 'yyyyMMdd_HHmmss') as run_date
    FROM interval_data_files_xfrm_vw
    WHERE data_type = 'new' AND aep_opco = '{opco}'
    """)

    print(insert_downstream_sql)
    spark.sql(insert_downstream_sql)
    print(f'completed Step 10 INSERT downstream in {str(timedelta(seconds=time.time() - _time))}')

    # delete scratch directory (includes raw_staging + dedup_xfrm_stage)
    hadoop_conf = spark._jsc.hadoopConfiguration()
    fs = spark._jvm.org.apache.hadoop.fs.FileSystem.get(hadoop_conf)
    scratch_hdfs_path = spark._jvm.org.apache.hadoop.fs.Path(scratch_path)
    if fs.exists(scratch_hdfs_path):
        print(f"Deleting HDFS scratch path: {scratch_hdfs_path}")
        fs.delete(scratch_hdfs_path, True)

    spark.stop()
    print(f'===== Total files processed: {files_count} =====')
    print(f'completed job in {str(timedelta(seconds=time.time() - _bgn_time))}')

if __name__ == "__main__":
    main()
