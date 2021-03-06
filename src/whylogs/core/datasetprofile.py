"""
Defines the primary interface class for tracking dataset statistics.
"""
import datetime
import io
from collections import OrderedDict
from uuid import uuid4

import numpy as np
import pandas as pd
import typing

from whylogs.core import ColumnProfile
from whylogs.core.types.typeddataconverter import TYPES
from whylogs.proto import (
    ColumnsChunkSegment,
    DatasetMetadataSegment,
    DatasetProfileMessage,
    DatasetProperties,
    DatasetSummary,
    MessageSegment,
)
from whylogs.util import time
from whylogs.util.data import getter, remap
from whylogs.util.dsketch import FrequentNumbersSketch
from whylogs.util.time import from_utc_ms, to_utc_ms
from google.protobuf.internal.decoder import _DecodeVarint32
from google.protobuf.internal.encoder import _VarintBytes

COLUMN_CHUNK_MAX_LEN_IN_BYTES = (
    int(1e6) - 10
)  #: Used for chunking serialized dataset profile messages
TYPENUM_COLUMN_NAMES = OrderedDict()
for k in TYPES.keys():
    TYPENUM_COLUMN_NAMES[k] = "type_" + k.lower() + "_count"

# NOTE: I use ordered dicts here to control the ordering of generated columns
# dictionaries are also valid
#: Define (some of) the mapping from dataset summary to flat table
SCALAR_NAME_MAPPING = OrderedDict(
    counters=OrderedDict(
        count="count",
        null_count=OrderedDict(value="null_count"),
        true_count=OrderedDict(value="bool_count"),
    ),
    number_summary=OrderedDict(
        count="numeric_count",
        max="max",
        mean="mean",
        min="min",
        stddev="stddev",
        unique_count=OrderedDict(
            estimate="nunique_numbers",
            lower="nunique_numbers_lower",
            upper="nunique_numbers_upper",
        ),
    ),
    schema=OrderedDict(
        inferred_type=OrderedDict(type="inferred_dtype", ratio="dtype_fraction"),
        type_counts=TYPENUM_COLUMN_NAMES,
    ),
    string_summary=OrderedDict(
        unique_count=OrderedDict(
            estimate="nunique_str",
            lower="nunique_str_lower",
            upper="ununique_str_upper",
        )
    ),
)


class DatasetProfile:
    """
    Statistics tracking for a dataset.

    A dataset refers to a collection of columns.

    Parameters
    ----------
    name: str
        A human readable name for the dataset profile. Could be model name.
        This is stored under "name" tag
    data_timestamp: datetime.datetime
        The timestamp associated with the data (i.e. batch run). Optional.
    session_timestamp : datetime.datetime
        Timestamp of the dataset
    columns : dict
        Dictionary lookup of `ColumnProfile`s
    tags : dict
        A dictionary of key->value. Can be used upstream for aggregating data. Tags must match when merging
        with another dataset profile object.
    metadata: dict
        Metadata that can store abirtrary string mapping. Metadata is not used when aggregating data
        and can be dropped when merging with another dataset profile object.
    session_id : str
        The unique session ID run. Should be a UUID.
    """

    def __init__(
        self,
        name: str,
        data_timestamp: datetime.datetime = None,
        session_timestamp: datetime.datetime = None,
        columns: dict = None,
        tags: typing.Dict[str, str] = None,
        metadata: typing.Dict[str, str] = None,
        session_id: str = None,
    ):
        # Default values
        if columns is None:
            columns = {}
        if tags is None:
            tags = dict()
        if metadata is None:
            metadata = dict()
        if session_id is None:
            session_id = uuid4().hex

        self.session_id = session_id
        self.session_timestamp = session_timestamp
        self.data_timestamp = data_timestamp
        self._tags = dict(tags)
        self._metadata = metadata.copy()
        self.columns = columns

        # Store Name attribute
        self._tags["Name"] = name

    @property
    def name(self):
        return self._tags["Name"]

    @property
    def tags(self):
        return self._tags.copy()

    @property
    def metadata(self):
        return self._metadata.copy()

    @property
    def session_timestamp(self):
        return self._session_timestamp

    @session_timestamp.setter
    def session_timestamp(self, x):
        if x is None:
            x = datetime.datetime.now(datetime.timezone.utc)
        assert isinstance(x, datetime.datetime)
        self._session_timestamp = x

    @property
    def session_timestamp_ms(self):
        """
        Return the session timestamp value in epoch milliseconds
        """
        return time.to_utc_ms(self.session_timestamp)

    def track(self, columns, data=None):
        """
        Add value(s) to tracking statistics for column(s)

        Parameters
        ----------
        columns : str, dict
            Either the name of a column, or a dictionary specifying column
            names and the data (value) for each column
            If a string, `data` must be supplied.  Otherwise, `data` is
            ignored.
        data : object, None
            Value to track.  Specify if `columns` is a string.
        """
        if data is not None:
            self._track_single_column(columns, data)
        else:
            for column_name, data in columns.items():
                self._track_single_column(column_name, data)

    def _track_single_column(self, column_name, data):
        try:
            prof = self.columns[column_name]
        except KeyError:
            prof = ColumnProfile(column_name)
            self.columns[column_name] = prof
        prof.track(data)

    def track_array(self, x: np.ndarray, columns=None):
        """
        Track statistics for a numpy array

        Parameters
        ----------
        x : np.ndarray
            2D array to track.
        columns : list
            Optional column labels
        """
        x = np.asanyarray(x)
        if np.ndim(x) != 2:
            raise ValueError("Expected 2 dimensional array")
        if columns is None:
            columns = np.arange(x.shape[1])
        columns = [str(c) for c in columns]
        return self.track_dataframe(pd.DataFrame(x, columns=columns))

    def track_dataframe(self, df: pd.DataFrame):
        """
        Track statistics for a dataframe

        Parameters
        ----------
        df : pandas.DataFrame
            DataFrame to track
        """
        for col in df.columns:
            col_str = str(col)
            x = df[col].values
            for xi in x:
                self.track(col_str, xi)

    def to_properties(self):
        """
        Return dataset profile related metadata

        Returns
        -------
        properties : DatasetProperties
            The metadata as a protobuf object.
        """
        tags = self.tags
        metadata = self.metadata
        if len(metadata) < 1:
            metadata = None

        session_timestamp = to_utc_ms(self.session_timestamp)
        data_timestamp = to_utc_ms(self.data_timestamp)

        return DatasetProperties(
            schema_major_version=1,
            schema_minor_version=1,
            session_id=self.session_id,
            session_timestamp=session_timestamp,
            data_timestamp=data_timestamp,
            tags=tags,
            metadata=metadata,
        )

    def to_summary(self):
        """
        Generate a summary of the statistics

        Returns
        -------
        summary : DatasetSummary
            Protobuf summary message.
        """
        self.validate()
        column_summaries = {
            name: colprof.to_summary() for name, colprof in self.columns.items()
        }

        return DatasetSummary(
            properties=self.to_properties(), columns=column_summaries,
        )

    def flat_summary(self):
        """
        Generate and flatten a summary of the statistics.

        See :func:`flatten_summary` for a description


        """
        summary = self.to_summary()
        return flatten_summary(summary)

    def _column_message_iterator(self):
        self.validate()
        for col in self.columns.items():
            yield col.to_protobuf()

    def chunk_iterator(self):
        """
        Generate an iterator to iterate over chunks of data
        """
        # Generate unique identifier
        marker = self.session_id + str(uuid4())

        # Generate metadata
        properties = self.to_properties()

        yield MessageSegment(
            marker=marker, metadata=DatasetMetadataSegment(properties=properties,)
        )

        chunked_columns = self._column_message_iterator()
        for msg in columns_chunk_iterator(chunked_columns, marker):
            yield MessageSegment(columns=msg)

    def validate(self):
        """
        Sanity check for this object.  Raises an AssertionError if invalid
        """
        for attr in (
            "name",
            "session_id",
            "session_timestamp",
            "columns",
            "tags",
            "metadata",
        ):
            assert getattr(self, attr) is not None
        assert all(isinstance(tag, str) for tag in self.tags.values())

    def merge(self, other):
        """
        Merge this profile with another dataset profile object.

        This operation will drop the metadata from the 'other' profile object.

        Parameters
        ----------
        other : DatasetProfile

        Returns
        -------
        merged : DatasetProfile
            New, merged DatasetProfile
        """
        self.validate()
        other.validate()

        assert self.session_id == other.session_id
        assert self.session_timestamp == other.session_timestamp
        assert self.data_timestamp == other.data_timestamp
        assert self.tags == other.tags

        columns_set = set(list(self.columns.keys()) + list(other.columns.keys()))
        columns = {}
        for col_name in columns_set:
            empty_column = ColumnProfile(col_name)
            this_column = self.columns.get(col_name, empty_column)
            other_column = other.columns.get(col_name, empty_column)
            columns[col_name] = this_column.merge(other_column)

        return DatasetProfile(
            name=self.name,
            session_id=self.session_id,
            session_timestamp=self.session_timestamp,
            data_timestamp=self.data_timestamp,
            columns=columns,
            tags=self.tags,
            metadata=self.metadata,
        )

    def serialize_delimited(self) -> bytes:
        """
        Write out in delimited format (data is prefixed with the length of the
        datastream).

        This is useful when you are streaming multiple dataset profile objects

        Returns
        -------
        data : bytes
            A sequence of bytes
        """
        with io.BytesIO() as f:
            protobuf: DatasetProfileMessage = self.to_protobuf()
            size = protobuf.ByteSize()
            f.write(_VarintBytes(size))
            f.write(protobuf.SerializeToString(deterministic=True))
            return f.getvalue()

    def to_protobuf(self) -> DatasetProfileMessage:
        """
        Return the object serialized as a protobuf message

        Returns
        -------
        message : DatasetProfileMessage
        """
        properties = self.to_properties()

        return DatasetProfileMessage(
            properties=properties,
            columns={k: v.to_protobuf() for k, v in self.columns.items()},
        )

    @staticmethod
    def from_protobuf(message: DatasetProfileMessage):
        """
        Load from a protobuf message

        Parameters
        ----------
        message : DatasetProfileMessage
            The protobuf message.  Should match the output of
            `DatasetProfile.to_protobuf()`

        Returns
        -------
        dataset_profile : DatasetProfile
        """
        return DatasetProfile(
            name=message.properties.tags["Name"],
            session_id=message.properties.session_id,
            session_timestamp=from_utc_ms(message.properties.session_timestamp),
            data_timestamp=from_utc_ms(message.properties.data_timestamp),
            columns={
                k: ColumnProfile.from_protobuf(v) for k, v in message.columns.items()
            },
            tags=dict(message.properties.tags),
            metadata=dict(message.properties.metadata),
        )

    @staticmethod
    def from_protobuf_string(data: bytes):
        """
        Deserialize a serialized `DatasetProfileMessage`

        Parameters
        ----------
        data : bytes
            The serialized message

        Returns
        -------
        profile : DatasetProfile
            The deserialized dataset profile
        """
        msg = DatasetProfileMessage.FromString(data)
        return DatasetProfile.from_protobuf(msg)

    @staticmethod
    def _parse_delimited_generator(data: bytes):
        pos = 0
        data_len = len(data)
        while pos < data_len:
            pos, profile = DatasetProfile.parse_delimited_single(data, pos)
            yield profile

    @staticmethod
    def parse_delimited_single(data: bytes, pos=0):
        """
        Parse a single delimited entry from a byte stream
        Parameters
        ----------
        data : bytes
            The bytestream
        pos : int
            The starting position. Default is zero

        Returns
        -------
        pos : int
            Current position in the stream after parsing
        profile : DatasetProfile
            A dataset profile
        """
        msg_len, new_pos = _DecodeVarint32(data, pos)
        pos = new_pos
        msg_buf = data[pos : pos + msg_len]
        pos += msg_len
        profile = DatasetProfile.from_protobuf_string(msg_buf)
        return pos, profile

    @staticmethod
    def parse_delimited(data: bytes):
        """
        Parse delimited data (i.e. data prefixed with the message length).

        Java protobuf writes delimited messages, which is convenient for
        storing multiple dataset profiles. This means that the main data is
        prefixed with the length of the message.

        Parameters
        ----------
        data : bytes
            The input byte stream

        Returns
        -------
        profiles : list
            List of all Dataset profile objects

        """
        return list(DatasetProfile._parse_delimited_generator(data))


def columns_chunk_iterator(iterator, marker: str):
    """
    Create an iterator to return column messages in batches

    Parameters
    ----------
    iterator
        An iterator which returns protobuf column messages
    marker
        Value used to mark a group of column messages
    """
    # Initialize
    max_len = COLUMN_CHUNK_MAX_LEN_IN_BYTES
    content_len = 0
    message = ColumnsChunkSegment(marker=marker)

    # Loop over columns
    for col_message in iterator:
        message_len = col_message.ByteSize()
        candidate_content_size = content_len + message_len
        if candidate_content_size <= max_len:
            # Keep appending columns
            message.columns.append(col_message)
            content_len = candidate_content_size
        else:
            yield message
            message = ColumnsChunkSegment(marker=marker)
            message.columns.append(col_message)
            content_len = message_len

    # Take care of any remaining messages
    if len(message.columns) > 0:
        yield message


def flatten_summary(dataset_summary: DatasetSummary) -> dict:
    """
    Flatten a DatasetSummary

    Parameters
    ----------
    dataset_summary : DatasetSummary
        Summary to flatten

    Returns
    -------
    data : dict
        A dictionary with the following keys:

            summary : pandas.DataFrame
                Per-column summary statistics
            hist : pandas.Series
                Series of histogram Series with (column name, histogram) key,
                value pairs.  Histograms are formatted as a `pandas.Series`
            frequent_strings : pandas.Series
                Series of frequent string counts with (column name, counts)
                key, val pairs.  `counts` are a pandas Series.

    Notes
    -----
    Some relevant info on the summary mapping:

    .. code-block:: python

        >>> from whylogs.core.datasetprofile import SCALAR_NAME_MAPPING
        >>> import json
        >>> print(json.dumps(SCALAR_NAME_MAPPING, indent=2))
    """
    hist = flatten_dataset_histograms(dataset_summary)
    frequent_strings = flatten_dataset_frequent_strings(dataset_summary)
    frequent_numbers = flatten_dataset_frequent_numbers(dataset_summary)
    summary = get_dataset_frame(dataset_summary)
    return {
        "summary": summary,
        "hist": hist,
        "frequent_strings": frequent_strings,
        "frequent_numbers": frequent_numbers,
    }


def _quantile_strings(quantiles: list):
    return ["quantile_{:.4f}".format(q) for q in quantiles]


def flatten_dataset_quantiles(dataset_summary: DatasetSummary):
    """
    Flatten quantiles from a dataset summary
    """
    quants = {}
    for col_name, col in dataset_summary.columns.items():
        try:
            quant = getter(getter(col, "number_summary"), "quantiles")
            x = OrderedDict()
            for q, qval in zip(
                _quantile_strings(quant.quantiles), quant.quantile_values
            ):
                x[q] = qval
            quants[col_name] = x
        except KeyError:
            pass
    return quants


def flatten_dataset_histograms(dataset_summary: DatasetSummary):
    """
    Flatten histograms from a dataset summary
    """
    histograms = {}

    for col_name, col in dataset_summary.columns.items():
        try:
            hist = getter(getter(col, "number_summary"), "histogram")
            if len(hist.bins) > 1:
                histograms[col_name] = {
                    "bin_edges": list(hist.bins),
                    "counts": list(hist.counts),
                }
        except KeyError:
            continue
    return histograms


def flatten_dataset_frequent_numbers(dataset_summary: DatasetSummary):
    """
    Flatten frequent number counts from a dataset summary
    """
    frequent_numbers = {}

    for col_name, col in dataset_summary.columns.items():
        try:
            summary = getter(getter(col, "number_summary"), "frequent_numbers")
            flat_dict = FrequentNumbersSketch.flatten_summary(summary)
            if len(flat_dict) > 0:
                frequent_numbers[col_name] = flat_dict
        except KeyError:
            continue
    return frequent_numbers


def flatten_dataset_frequent_strings(dataset_summary: DatasetSummary):
    """
    Flatten frequent strings summaries from a dataset summary
    """
    frequent_strings = {}

    for col_name, col in dataset_summary.columns.items():
        try:
            item_summary = getter(getter(col, "string_summary"), "frequent").items
            items = {}
            for item in item_summary:
                items[item.value] = int(item.estimate)
            if len(items) > 0:
                frequent_strings[col_name] = items
        except KeyError:
            continue

    return frequent_strings


def get_dataset_frame(dataset_summary: DatasetSummary, mapping: dict = None):
    """
    Get a dataframe from scalar values flattened from a dataset summary

    Parameters
    ----------
    dataset_summary : DatasetSummary
        The dataset summary.
    mapping : dict, optional
        Override the default variable mapping.

    Returns
    -------
    summary : pd.DataFrame
        Scalar values, flattened and re-named according to `mapping`
    """
    if mapping is None:
        mapping = SCALAR_NAME_MAPPING
    quantile = flatten_dataset_quantiles(dataset_summary)
    col_out = {}
    for _k, col in dataset_summary.columns.items():
        col_out[_k] = remap(col, mapping)
        col_out[_k].update(quantile.get(_k, {}))
    scalar_summary = pd.DataFrame(col_out).T
    scalar_summary.index.name = "column"
    return scalar_summary.reset_index()


def dataframe_profile(
    df: pd.DataFrame, name: str = None, timestamp: datetime.datetime = None
):
    """
    Generate a dataset profile for a dataframe

    Parameters
    ----------
    df : pandas.DataFrame
        Dataframe to track, treated as a complete dataset.
    name : str
        Name of the dataset
    timestamp : datetime.datetime, float
        Timestamp of the dataset.  Defaults to current UTC time.  Can be a
        datetime or UTC epoch seconds.

    Returns
    -------
    prof : DatasetProfile
    """
    if name is None:
        name = "dataset"
    if timestamp is None:
        timestamp = datetime.datetime.utcnow()
    elif not isinstance(timestamp, datetime.datetime):
        # Assume UTC epoch seconds
        timestamp = datetime.datetime.utcfromtimestamp(float(timestamp))
    prof = DatasetProfile(name, timestamp)
    prof.track_dataframe(df)
    return prof


def array_profile(
    x: np.ndarray,
    name: str = None,
    timestamp: datetime.datetime = None,
    columns: list = None,
):
    """
    Generate a dataset profile for an array

    Parameters
    ----------
    x : np.ndarray
        Array-like object to track.  Will be treated as an full dataset
    name : str
        Name of the dataset
    timestamp : datetime.datetime
        Timestamp of the dataset.  Defaults to current UTC time
    columns : list
        Optional column labels

    Returns
    -------
    prof : DatasetProfile
    """
    if name is None:
        name = "dataset"
    if timestamp is None:
        timestamp = datetime.datetime.utcnow()
    prof = DatasetProfile(name, timestamp)
    prof.track_array(x, columns)
    return prof
