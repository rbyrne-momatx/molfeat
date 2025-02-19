from typing import Dict, Iterable, List
from typing import Any
from typing import Union
from typing import Optional
from typing import Callable
from typing import Mapping

import ast
import abc
import atexit
import copy
import glob
import pathlib
import uuid
import shelve
import platformdirs
import h5py
import os
import fsspec
import numpy as np
import pandas as pd
import datamol as dm
import joblib
import itertools
import random
import multiprocessing as mp

import pandas.errors

from functools import partial
from rdkit.Chem import rdchem
from molfeat.utils import commons
from molfeat.utils import datatype


class MolToKey:
    """Convert a molecule to a key"""

    SUPPORTED_HASH_FN = {
        "dm.unique_id": dm.unique_id,
        "dm.to_inchikey": dm.to_inchikey,
    }

    def __init__(self, hash_fn: Optional[Union[Callable, str]] = "dm.unique_id"):
        """Init function for molecular key generator.

        Args:
            hash_fn: hash function to use for the molecular key
        """

        if isinstance(hash_fn, str):
            if hash_fn not in self.SUPPORTED_HASH_FN:
                raise ValueError(
                    f"Hash function {hash_fn} is not supported. "
                    f"Supported hash functions are: {self.SUPPORTED_HASH_FN.keys()}"
                )

            self.hash_name = hash_fn
            self.hash_fn = self.SUPPORTED_HASH_FN[hash_fn]

        else:
            self.hash_fn = hash_fn
            self.hash_name = None

            if self.hash_fn is None:
                self.hash_fn = dm.unique_id
                self.hash_name = "dm.unique_id"

    def __call__(self, mol: rdchem.Mol):
        """Convert a molecule object to a key that can be used for the cache system

        Args:
            mol: input molecule object
        """
        is_mol = dm.to_mol(mol) is not None

        if is_mol and self.hash_fn is not None:
            return self.hash_fn(mol)

        return mol

    def to_state_dict(self):
        """Serialize MolToKey to a state dict."""

        if self.hash_name is None:
            raise ValueError(
                "The hash function has been provided as a function and not a string. "
                "So it's impossible to save the state. You must specifiy the hash function as a string instead."
            )

        state = {}
        state["hash_name"] = self.hash_name
        return state

    @staticmethod
    def from_state_dict(state: dict) -> "MolToKey":
        """Load a MolToKey object from a state dict."""
        return MolToKey(hash_fn=state["hash_name"])


class _Cache(abc.ABC):

    """Implementation of a cache interface"""

    def __init__(
        self,
        mol_hasher: Optional[Union[Callable, str, MolToKey]] = None,
        name: Optional[str] = None,
        n_jobs: Optional[int] = -1,
        verbose: Union[bool, int] = False,
    ):
        """
        Constructor for the Cache system

        Args:
            mol_hasher: function to use to hash molecules. If not provided, inchikey is used by default
            name: name of the cache, will be autogenerated if not provided
            n_jobs: number of parallel jobs to use when performing any computation
            verbose: whether to print progress. Default to False
        """
        self.name = name or str(uuid.uuid4())
        self.n_jobs = n_jobs
        self.verbose = verbose

        if isinstance(mol_hasher, MolToKey):
            self.mol_hasher = mol_hasher
        else:
            self.mol_hasher = MolToKey(mol_hasher)

        self.cache = {}

    def __getitem__(self, key):
        key = self.mol_hasher(key)
        return self.cache[key]

    def __contains__(self, key: Any):
        """Check whether a key is in the cache
        Args:
            key: key to check in the cache
        """
        key = self.mol_hasher(key)
        return key in self.cache

    def __len__(self):
        """Return the length of the cache"""
        return len(self.keys())

    def __iter__(self):
        """Iterate over the cache"""
        return iter(self.cache)

    def __setitem__(self, key: Any, item: Any):
        """Add an item to the cache

        Args:
            key: input key to set
            item: value of the key to set
        """
        self.update({key: item})

    def __call__(
        self,
        mols: List[Union[rdchem.Mol, str]],
        featurizer: Any,
        enforce_dtype=True,
        **transform_kwargs,
    ):
        """
        Compute the features for a list of molecules and save them to the cache

        Args:
            mols: list of molecule to preprocess
            featurizer: input featurizer to use to compute the molecular representation. Should implement a `__call__` method.
            transformer_kwargs: keyword arguments to pass to the transformer.

        !!! note
            Parquet format does not support tensor datatype, so you should ensure that the output of
            the featurizer is a numpy array in that case

        Returns:
            processed: list of computed features for input molecules
        """

        converter = copy.deepcopy(self.mol_hasher)
        mol_ids = dm.parallelized(
            converter,
            mols,
            n_jobs=self.n_jobs,
            progress=self.verbose,
            tqdm_kwargs=dict(leave=False),
        )

        # only recompute on unseen ids
        unseen_ids = []
        mol_queries = []
        for mol_id, m in zip(mol_ids, mols):
            if mol_id not in self:
                unseen_ids.append(mol_id)
                mol_queries.append(m)
        if len(mol_queries) > 0:
            features = featurizer(
                mol_queries,
                **transform_kwargs,
            )
            dtype = getattr(featurizer, "dtype", None)
            if dtype is not None:
                features = datatype.cast(features, dtype=dtype)
            for key, feat in zip(unseen_ids, features):
                self[key] = feat
            self._sync_cache()
        return self.fetch(mols)

    def clear(self, *args, **kwargs):
        ...

    @abc.abstractmethod
    def update(self, new_cache: Mapping[Any, Any]):
        ...

    def get(self, key, default: Optional[Any] = None):
        """Get the cached value for a specific key
        Args:
            key: key to get
            default: default value to return when the key is not found
        """
        key = self.mol_hasher(key)
        return self.cache.get(key, default)

    def keys(self):
        """Get list of keys in the cache"""
        return self.cache.keys()

    def values(self):
        """Get list of values in the cache"""
        return self.cache.values()

    def items(self):
        """Return iterator of key, values in the cache"""
        return self.cache.items()

    def to_dict(self):
        """Convert current cache to a dictionary"""
        return dict(self.items())

    def _sync_cache(self):
        ...

    def fetch(
        self,
        mols: List[Union[rdchem.Mol, str]],
    ):
        """Get the representation for a single

        Args:
            mols: list of molecules
        """
        if isinstance(mols, str) or not isinstance(mols, Iterable):
            mols = [mols]

        converter = copy.deepcopy(self.mol_hasher)
        mol_ids = dm.parallelized(
            converter,
            mols,
            n_jobs=self.n_jobs,
            progress=self.verbose,
            tqdm_kwargs=dict(leave=False),
        )
        return [self.get(mol_id) for mol_id in mol_ids]

    @abc.abstractclassmethod
    def load_from_file(cls, filepath: Union[os.PathLike, str], **kwargs):
        """Load a cache from a file (including remote file)

        Args:
            filepath: path to the file to load
            kwargs: keyword arguments to pass to the constructor
        """
        ...

    @abc.abstractmethod
    def save_to_file(self, filepath: Union[os.PathLike, str]):
        """Save the cache to a file

        Args:
            filepath: path to the file to save
        """
        ...


class DataCache(_Cache):
    """
    Molecular features caching system that cache computed values in memory for reuse later
    """

    def __init__(
        self,
        name: str,
        n_jobs: int = -1,
        mol_hasher: Optional[Union[Callable, str, MolToKey]] = None,
        verbose: Union[bool, int] = False,
        cache_file: Optional[Union[os.PathLike, bool]] = None,
        delete_on_exit: bool = False,
        clear_on_exit: bool = True,
    ):
        """Precomputed fingerprint caching callback

        Args:
            name: name of the cache
            n_jobs: number of parallel jobs to use when performing any computation
            mol_hasher: function to use to hash molecules. If not provided, `dm.unique_id`` is used by default
            verbose: whether to print progress. Default to False
            cache_file: Cache location. Defaults to None, which will use in-memory caching.
            delete_on_exit: Whether to delete the cache file on exit. Defaults to False.
            clear_on_exit: Whether to clear the cache on exit of the interpreter. Default to True
        """
        super().__init__(name=name, mol_hasher=mol_hasher, n_jobs=n_jobs, verbose=verbose)

        if cache_file is True:
            cache_file = pathlib.Path(
                platformdirs.user_cache_dir(appname="molfeat")
            ) / "precomputed/{}_{}.db".format(self.name, str(uuid.uuid4())[:8])

            cache_file = str(cache_file)
        self.cache_file = cache_file
        self.cache = {}
        self._initialize_cache()
        self.delete_on_exit = delete_on_exit
        self.clear_on_exit = clear_on_exit
        if self.clear_on_exit:
            atexit.register(partial(self.clear, delete=delete_on_exit))

    def _initialize_cache(self):
        if self.cache_file not in [None, False]:
            # force creation of cache directory
            cache_parent = pathlib.Path(self.cache_file).parent
            cache_parent.mkdir(parents=True, exist_ok=True)
            self.cache = shelve.open(self.cache_file)
        else:
            self.cache = {}

    def clear(self, delete: bool = False):
        """Clear cache memory if needed.
        Note that a cleared cache cannot be used anymore

        Args:
            delete: whether to delete the cache file if on disk
        """
        self.cache.clear()
        if isinstance(self.cache, shelve.Shelf):
            self.cache.close()
            # EN: temporary set it to a dict before reopening
            # this needs to be done to prevent operating on close files
            self.cache = {}
        if delete:
            if self.cache_file is not None:
                for path in glob.glob(str(self.cache_file) + "*"):
                    try:
                        os.unlink(path)
                    except:
                        pass
        else:
            self._initialize_cache()

    def update(self, new_cache: Mapping[Any, Any]):
        """Update the cache with new values

        Args:
            new_cache: new cache with items to use to update current cache
        """
        for k, v in new_cache.items():
            k = self.mol_hasher(k)
            self.cache[k] = v
        return self

    def _sync_cache(self):
        """Perform a cache sync to ensure values are up to date"""
        if isinstance(self.cache, shelve.Shelf):
            self.cache.sync()

    @classmethod
    def load_from_file(cls, filepath: Union[os.PathLike, str]):
        """Load a datache from a file (including remote file)

        Args:
            filepath: path to the file to load
        """
        cached_data = None
        with fsspec.open(filepath, "rb") as f:
            cached_data = joblib.load(f)
        data = cached_data.pop("data", {})
        new_cache = cls(**cached_data)
        new_cache.update(data)
        return new_cache

    def save_to_file(self, filepath: Union[os.PathLike, str]):
        """Save the cache to a file

        Args:
            filepath: path to the file to save
        """
        information = dict(
            name=self.name,
            n_jobs=self.n_jobs,
            mol_hasher=self.mol_hasher,
            verbose=self.verbose,
            cache_file=(self.cache_file is not None),
            delete_on_exit=self.delete_on_exit,
        )
        information["data"] = self.to_dict()
        with fsspec.open(filepath, "wb") as f:
            joblib.dump(information, f)


class MPDataCache(DataCache):
    """A datacache that supports multiprocessing natively"""

    def __init__(
        self,
        name: Optional[str] = None,
        n_jobs: int = -1,
        mol_hasher: Optional[Union[Callable, str, MolToKey]] = None,
        verbose: Union[bool, int] = False,
        clear_on_exit: bool = False,
    ):
        """Multiprocessing datacache that save cache into a shared memory

        Args:
            name: name of the cache
            n_jobs: number of parallel jobs to use when performing any computation
            mol_hasher: function to use to hash molecules. If not provided, `dm.unique_id`` is used by default
            verbose: whether to print progress. Default to False
            clear_on_exit: Whether to clear the cache on exit. Default is False to allow sharing the cache content
        """
        super().__init__(
            name=name,
            n_jobs=n_jobs,
            mol_hasher=mol_hasher,
            cache_file=None,
            verbose=verbose,
            delete_on_exit=False,
            clear_on_exit=clear_on_exit,
        )

    def _initialize_cache(self):
        """Initialize empty cache using a shared dict"""
        manager = mp.Manager()  # this might not be a great idea to initialize everytime...
        self.cache = manager.dict()


class FileCache(_Cache):
    """
    Read only cache that holds in precomputed data in a pickle, csv or h5py file.

    The convention used requires the 'keys' and  'values' columns when
    the input file needs to be loaded as a dataframe.
    """

    _PICKLE_PROTOCOL = 4
    SUPPORTED_TYPES = ["pickle", "pkl", "csv", "parquet", "pq", "hdf5", "h5"]

    def __init__(
        self,
        cache_file: Union[os.PathLike, str],
        name: Optional[str] = None,
        mol_hasher: Optional[Union[Callable, str, MolToKey]] = None,
        n_jobs: Optional[int] = None,
        verbose: Union[bool, int] = False,
        file_type: str = "parquet",
        clear_on_exit: bool = True,
        parquet_kwargs: Optional[Dict[Any, Any]] = None,
    ):
        """Precomputed fingerprint caching callback

        !!! note
            Do not pickle this object, instead use the provided saving methods.

        Args:
            cache_file: Cache location. Can be a local file or a remote file
            name: optional name to give the cache
            mol_hasher: function to use to hash molecules. If not provided, `dm.unique_id` is used by default
            n_jobs: number of parallel jobs to use when performing any computation
            verbose: whether to print information about the cache
            clear_on_exit: whether to clear the cache on exit of the interpreter
            file_type: File type that was provided. One of "csv", "pickle", "hdf5" and "parquet"
                For "csv" and "parquet", we expect columns "keys" and "values"
                For a pickle, we expect either a mapping or a dataframe with "keys" and "values" columns
            parquet_kwargs: Argument to pass to the parquet reader.
        """
        super().__init__(name=name, mol_hasher=mol_hasher, n_jobs=n_jobs, verbose=verbose)

        self.cache_file = cache_file
        self.file_type = file_type
        self.parquet_kwargs = parquet_kwargs or {}
        self.clear_on_exit = clear_on_exit

        if self.file_type not in FileCache.SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported file type, expected one of {FileCache.SUPPORTED_TYPES}, got '{self.file_type}'"
            )

        if self.cache_file is not None and dm.fs.exists(self.cache_file):
            self._load_cache()
        else:
            self.cache = {}

        if self.clear_on_exit:
            atexit.register(self.clear)

    def clear(self):
        """Clear cache memory at exit and close any open file
        Note that a cleared cache cannot be used anymore !
        """
        if self.file_type in ["hdf5", "h5"]:
            self.cache.close()
        else:
            del self.cache
        # reset cache to empty
        self.cache = {}

    def items(self):
        """Return iterator of key, values in the cache"""
        if self.file_type in ["hdf5", "h5"]:
            return ((k, np.asarray(v)) for k, v in self.cache.items())
        return super().items()

    def _load_cache(self):
        """Load cache internally if needed"""

        file_exists = dm.utils.fs.exists(self.cache_file)

        if self.file_type in ["hdf5", "h5"]:
            f = fsspec.open("simplecache::" + self.cache_file, "rb+").open()
            self.cache = h5py.File(f, "r+")

        elif not file_exists:
            self.cache = {}

        elif self.file_type in ["pickle", "pkl"]:
            with fsspec.open(self.cache_file, "rb") as IN:
                self.cache = joblib.load(IN)

        elif self.file_type == "csv":
            with fsspec.open(self.cache_file, "rb") as IN:
                # Allow the CSV file to exist but with an empty content
                try:
                    self.cache = pd.read_csv(
                        IN,
                        converters={"values": lambda x: commons.unpack_bits(ast.literal_eval(x))},
                    )
                except pandas.errors.EmptyDataError:
                    self.cache = {}

        elif self.file_type in ["parquet", "pq"]:
            self.cache = pd.read_parquet(
                self.cache_file,
                columns=["keys", "values"],
                **self.parquet_kwargs,
            )
        # convert dataframe to dict if needed
        if isinstance(self.cache, pd.DataFrame):
            self.cache = self.cache.set_index("keys").to_dict()["values"]

    def update(self, new_cache: Mapping[Any, Any]):
        """Update the cache with new values

        Args:
            new_cache: new cache with items to use to update current cache
        """
        for k, v in new_cache.items():
            key = self.mol_hasher(k)
            if self.file_type in ["hdf5", "h5"]:
                self.cache.create_dataset(key, data=v)
            else:
                self.cache[key] = v
        return self

    @classmethod
    def load_from_file(cls, filepath: Union[os.PathLike, str], **kwargs):
        """Load a FileCache from a file

        Args:
            filepath: path to the file to load
            kwargs: keyword arguments to pass to the constructor
        """
        new_cache = cls(cache_file=filepath, **kwargs)
        return new_cache

    def to_dataframe(self, pack_bits: bool = False):
        """Convert the cache to a dataframe. The converted dataframe would have `keys` and `values` columns

        Args:
            pack_bits: whether to pack the values columns into bits.
                By using molfeat.utils.commons.unpack_bits, the values column can be reloaded as an array
        """
        if pack_bits:
            loaded_items = [
                (k, commons.pack_bits(x, protocol=self._PICKLE_PROTOCOL)) for k, x in self.items()
            ]
        else:
            loaded_items = self.items()
        df = pd.DataFrame(loaded_items, columns=["keys", "values"])
        return df

    def save_to_file(
        self,
        filepath: Optional[Union[os.PathLike, str]] = None,
        file_type: Optional[str] = None,
        **kwargs,
    ):
        """Save the cache to a file

        Args:
            filepath: path to the file to save. If None, the cache is saved to the original file.
            file_type: format used to save the cache to file one of "pickle", "csv", "hdf5", "parquet".
                If None, the original file type is used.
            kwargs: keyword arguments to pass to the serializer to disk (e.g to pass to pd.to_csv or pd.to_parquet)
        """

        if filepath is None:
            filepath = self.cache_file

        if file_type is None:
            file_type = self.file_type

        if file_type in ["pkl", "pickle"]:
            with fsspec.open(filepath, "wb") as f:
                joblib.dump(self.to_dict(), f)

        elif file_type in ["csv", "parquet", "pq"]:
            df = self.to_dataframe(pack_bits=(file_type == "csv"))

            if file_type == "csv":
                with fsspec.open(filepath, "w") as f:
                    df.to_csv(f, index=False, **kwargs)
            else:
                df.to_parquet(filepath, index=False, **kwargs)

        elif file_type in ["hdf5", "h5"]:
            with fsspec.open(filepath, "wb") as IN:
                with h5py.File(IN, "w") as f:
                    for k, v in self.items():
                        f.create_dataset(k, data=v)
        else:
            raise ValueError("Unsupported output protocol: {}".format(file_type))

    def to_state_dict(self, save_to_file: bool = True) -> dict:
        """Serialize the cache to a state dict.

        Args:
            save_to_file: whether to save the cache to file.
        """

        if save_to_file is True:
            self.save_to_file()

        state = {}
        state["_cache_name"] = "FileCache"
        state["cache_file"] = self.cache_file
        state["name"] = self.name
        state["n_jobs"] = self.n_jobs
        state["verbose"] = self.verbose
        state["file_type"] = self.file_type
        state["clear_on_exit"] = self.clear_on_exit
        state["parquet_kwargs"] = self.parquet_kwargs
        state["mol_hasher"] = self.mol_hasher.to_state_dict()

        return state

    @staticmethod
    def from_state_dict(state: dict, override_args: Optional[dict] = None) -> "FileCache":
        # Don't alter the original state dict
        state = copy.deepcopy(state)

        cache_name = state.pop("_cache_name")

        if cache_name != "FileCache":
            raise ValueError(f"The cache object name is invalid: {cache_name}")

        # Load the MolToKey object
        state["mol_hasher"] = MolToKey.from_state_dict(state["mol_hasher"])

        if override_args is not None:
            state.update(override_args)

        return FileCache(**state)


class CacheList:
    """Proxy for supporting search using a list of cache"""

    def __init__(self, *caches):
        self.caches = caches

    def __getitem__(self, key):
        for cache in self.caches:
            val = cache.get(key)
            if val is not None:
                return val
        raise KeyError(f"{key} not found in any cache")

    def __contains__(self, key: Any):
        """Check whether a key is in the cache
        Args:
            key: key to check in the cache
        """
        return any(key in cache for cache in self.caches)

    def __len__(self):
        """Return the length of the cache"""
        return sum(len(c) for c in self.caches)

    def __iter__(self):
        """Iterate over all the caches"""
        return itertools.chain(*iter(self.cache))

    def __setitem__(self, key: Any, item: Any):
        """Add an item to the cache

        Args:
            key: input key to set
            item: value of the key to set
        """
        # select a random cache and add the item to the cache
        cache = random.choice(self.caches)
        cache.update({key: item})

    def __call__(self, *args, **kwargs):
        """
        Compute the features for a list of molecules and save them to the cache
        """

        raise NotImplementedError(
            "Dynamic updating of a cache list using a featurizer is not supported!"
        )

    def clear(self, *args, **kwargs):
        """Clear all the caches and make them inaccesible"""
        for cache in self.caches:
            cache.clear(*args, **kwargs)

    def update(self, new_cache: Mapping[Any, Any]):
        cache = random.choice(self.caches)
        cache.update(new_cache)

    def get(self, key, default: Optional[Any] = None):
        """Get the cached value for a specific key
        Args:
            key: key to get
            default: default value to return when the key is not found
        """
        for cache in self.caches:
            val = cache.get(key)
            if val is not None:
                return val
        return default

    def keys(self):
        """Get list of keys in the cache"""
        return list(itertools.chain(*(c.keys() for c in self.caches)))

    def values(self):
        """Get list of values in the cache"""
        return list(itertools.chain(*(c.values() for c in self.caches)))

    def items(self):
        """Return iterator of key, values in the cache"""
        return list(itertools.chain(*(c.items() for c in self.caches)))

    def to_dict(self):
        """Convert current cache to a dictionary"""
        return dict(self.items())

    def fetch(
        self,
        mols: List[Union[rdchem.Mol, str]],
    ):
        """Get the representation for a single

        Args:
            mols: list of molecules
        """
        if isinstance(mols, str) or not isinstance(mols, Iterable):
            mols = [mols]
        return [self.get(mol) for mol in mols]
