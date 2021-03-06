"""
"""
import os

import pandas as pd

from whylogs import DatasetProfile
from whylogs.app.config import SessionConfig, WriterConfig
from whylogs.app.session import get_or_create_session, session_from_config
from whylogs.app.writers import writer_from_config
from whylogs.util import time


def test_write_template_path():
    data_time = time.from_utc_ms(9999)
    session_time = time.from_utc_ms(88888)
    path_template = "$name-$session_timestamp-$dataset_timestamp-$session_id"
    writer_config = WriterConfig(
        "local", ["protobuf", "flat"], "output", path_template, "dataset-profile-$name"
    )
    writer = writer_from_config(writer_config)
    dp = DatasetProfile("name", data_time, session_time, session_id="session")
    assert writer.path_suffix(dp) == "name-88888-9999-session"
    assert writer.file_name(dp, ".txt") == "dataset-profile-name.txt"


def test_config_api(tmpdir):
    p = tmpdir.mkdir("whylogs")

    writer_config = WriterConfig("local", ["protobuf", "flat"], p.realpath())
    yaml_data = writer_config.to_yaml()
    WriterConfig.from_yaml(yaml_data)

    session_config = SessionConfig("project", "pipeline", writers=[writer_config])

    session = session_from_config(session_config)

    with session.logger("test_name") as logger:
        logger.log_dataframe(pd.DataFrame())
    session.close()


def test_load_config(tmpdir):
    original_dir = os.curdir

    p = tmpdir.mkdir("whylogs")

    try:
        session = get_or_create_session()
        os.chdir(p)
        assert session.project == "test-project-yaml"

        with session.logger("test_name") as logger:
            logger.log_dataframe(pd.DataFrame())
        session.close()
    finally:
        os.chdir(original_dir)


def test_log_dataframe(tmpdir, df_lending_club):
    p = tmpdir.mkdir("whylogs")

    writer_config = WriterConfig("local", ["protobuf", "flat"], p.realpath())
    yaml_data = writer_config.to_yaml()
    WriterConfig.from_yaml(yaml_data)

    session_config = SessionConfig("project", "pipeline", writers=[writer_config])
    session = session_from_config(session_config)

    with session.logger("lendingclub") as logger:
        logger.log_dataframe(df_lending_club)

    output_files = []
    for root, subdirs, files in os.walk(p):
        output_files += files
    assert len(output_files) == 5
