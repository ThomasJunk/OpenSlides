import functools
import hashlib
from collections import defaultdict
from textwrap import dedent
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

from django.core.exceptions import ImproperlyConfigured
from typing_extensions import Protocol

from . import logging
from .redis import use_redis
from .schema_version import SchemaVersion
from .utils import split_element_id, str_dict_to_bytes


logger = logging.getLogger(__name__)

if use_redis:
    from .redis import get_connection, aioredis


class CacheReset(Exception):
    pass


class ElementCacheProvider(Protocol):
    """
    Base class for cache provider.

    See RedisCacheProvider as reverence implementation.
    """

    def __init__(self, ensure_cache: Callable[[], Coroutine[Any, Any, None]]) -> None:
        ...

    async def ensure_cache(self) -> None:
        ...

    async def clear_cache(self) -> None:
        ...

    async def reset_full_cache(
        self, data: Dict[str, str], default_change_id: int
    ) -> None:
        ...

    async def data_exists(self) -> bool:
        ...

    async def get_all_data(self) -> Dict[bytes, bytes]:
        ...

    async def get_collection_data(self, collection: str) -> Dict[int, bytes]:
        ...

    async def get_element_data(self, element_id: str) -> Optional[bytes]:
        ...

    async def add_changed_elements(
        self, changed_elements: List[str], deleted_element_ids: List[str]
    ) -> int:
        ...

    async def get_data_since(
        self, change_id: int, max_change_id: int = -1
    ) -> Tuple[Dict[str, List[bytes]], List[str]]:
        ...

    async def set_lock(self, lock_name: str) -> bool:
        ...

    async def get_lock(self, lock_name: str) -> bool:
        ...

    async def del_lock(self, lock_name: str) -> None:
        ...

    async def get_current_change_id(self) -> int:
        ...

    async def get_lowest_change_id(self) -> int:
        ...

    async def get_schema_version(self) -> Optional[SchemaVersion]:
        ...

    async def set_schema_version(self, schema_version: SchemaVersion) -> None:
        ...


def ensure_cache_wrapper() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Wraps a cache function to ensure, that the cache is filled.
    When the function raises a CacheReset-Error the cache will be ensured (call
    to `ensure_cache`) and the method will be recalled. This is done, until the
    operation was successful.
    """

    def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapped(
            cache_provider: ElementCacheProvider, *args: Any, **kwargs: Any
        ) -> Any:
            success = False
            while not success:
                try:
                    result = await func(cache_provider, *args, **kwargs)
                    success = True
                except CacheReset:
                    logger.warn(
                        f"Redis was flushed before method '{func.__name__}'. Ensures cache now."
                    )
                    await cache_provider.ensure_cache()
            return result

        return wrapped

    return wrapper


class RedisCacheProvider:
    """
    Cache provider that loads and saves the data to redis.
    """

    full_data_cache_key: str = "full_data"
    change_id_cache_key: str = "change_id"
    schema_cache_key: str = "schema"

    # All lua-scripts used by this provider. Every entry is a Tuple (str, bool) with the
    # script and an ensure_cache-indicator. If the indicator is True, a short ensure_cache-script
    # will be prepended to the script which raises a CacheReset, if the full data cache is empty.
    # This requires the full_data_cache_key to be the first key given in `keys`!
    # All scripts are dedented and hashed for faster execution. Convention: The keys of this
    # member are the methods that needs these scripts.
    scripts = {
        "clear_cache": (
            "return redis.call('del', 'fake_key', unpack(redis.call('keys', ARGV[1])))",
            False,
        ),
        "get_all_data": ("return redis.call('hgetall', KEYS[1])", True),
        "get_collection_data": (
            """
            local cursor = 0
            local collection = {}
            repeat
                local result = redis.call('HSCAN', KEYS[1], cursor, 'MATCH', ARGV[1])
                cursor = tonumber(result[1])
                for _, v in pairs(result[2]) do
                    table.insert(collection, v)
                end
            until cursor == 0
            return collection
            """,
            True,
        ),
        "get_element_data": ("return redis.call('hget', KEYS[1], ARGV[1])", True),
        "add_changed_elements": (
            """
            -- Generate a new change_id
            local tmp = redis.call('zrevrangebyscore', KEYS[2], '+inf', '-inf', 'WITHSCORES', 'LIMIT', 0, 1)
            local change_id
            if next(tmp) == nil then
                -- The key does not exist
                return redis.error_reply("cache_reset")
            else
                change_id = tmp[2] + 1
            end

            local nc = tonumber(ARGV[1])
            local nd = tonumber(ARGV[2])

            local i, max
            -- Add changed_elements to the cache and sorted set (the first of the pairs)
            if (nc > 0) then
                max = 1 + nc
                redis.call('hmset', KEYS[1], unpack(ARGV, 3, max + 1))
                for i = 3, max, 2 do
                    redis.call('zadd', KEYS[2], change_id, ARGV[i])
                end
            end

            -- Delete deleted_element_ids and add them to sorted set
            if (nd > 0) then
                max = 2 + nc + nd
                redis.call('hdel', KEYS[1], unpack(ARGV, 3 + nc, max))
                for i = 3 + nc, max, 1 do
                    redis.call('zadd', KEYS[2], change_id, ARGV[i])
                end
            end

            return change_id
            """,
            True,
        ),
        "get_data_since": (
            """
            -- Get change ids of changed elements
            local element_ids = redis.call('zrangebyscore', KEYS[2], ARGV[1], ARGV[2])

            -- Save elements in array. Rotate element_id and element_json
            local elements = {}
            for _, element_id in pairs(element_ids) do
              table.insert(elements, element_id)
              table.insert(elements, redis.call('hget', KEYS[1], element_id))
            end
            return elements
            """,
            True,
        ),
    }

    def __init__(self, ensure_cache: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._ensure_cache = ensure_cache

        # hash all scripts and remove indentation.
        for key in self.scripts.keys():
            script, add_ensure_cache = self.scripts[key]
            script = dedent(script)
            if add_ensure_cache:
                script = (
                    dedent(
                        """
                        local exist = redis.call('exists', KEYS[1])
                        if (exist == 0) then
                            redis.log(redis.LOG_WARNING, "empty: "..KEYS[1])
                            return redis.error_reply("cache_reset")
                        end
                        """
                    )
                    + script
                )
            self.scripts[key] = (script, add_ensure_cache)
        self._script_hashes = {
            key: hashlib.sha1(script.encode()).hexdigest()
            for key, (script, _) in self.scripts.items()
        }

    async def ensure_cache(self) -> None:
        await self._ensure_cache()

    async def clear_cache(self) -> None:
        """
        Deleted all cache entries created with this element cache.
        """
        await self.eval("clear_cache", keys=[], args=["*"])

    async def reset_full_cache(
        self, data: Dict[str, str], default_change_id: int
    ) -> None:
        """
        Deletes the full_data_cache and write new data in it. Clears the change id key.
        Does not clear locks.
        """
        async with get_connection() as redis:
            tr = redis.multi_exec()
            tr.delete(self.change_id_cache_key)
            tr.delete(self.full_data_cache_key)
            tr.hmset_dict(self.full_data_cache_key, data)
            tr.zadd(
                self.change_id_cache_key, default_change_id, "_config:lowest_change_id"
            )
            await tr.execute()

    async def data_exists(self) -> bool:
        """
        Returns True, when there is data in the cache.
        """
        async with get_connection() as redis:
            return await redis.exists(self.full_data_cache_key) and bool(
                await redis.zrangebyscore(
                    self.change_id_cache_key, withscores=True, count=1, offset=0
                )
            )

    @ensure_cache_wrapper()
    async def get_all_data(self) -> Dict[bytes, bytes]:
        """
        Returns all data from the full_data_cache in a mapping from element_id to the element.
        """
        return await aioredis.util.wait_make_dict(
            self.eval("get_all_data", [self.full_data_cache_key])
        )

    @ensure_cache_wrapper()
    async def get_collection_data(self, collection: str) -> Dict[int, bytes]:
        """
        Returns all elements for a collection from the cache. The data is mapped
        from element_id to the element.
        """
        response = await self.eval(
            "get_collection_data", [self.full_data_cache_key], [f"{collection}:*"]
        )

        collection_data = {}
        for i in range(0, len(response), 2):
            _, id = split_element_id(response[i])
            collection_data[id] = response[i + 1]

        return collection_data

    @ensure_cache_wrapper()
    async def get_element_data(self, element_id: str) -> Optional[bytes]:
        """
        Returns one element from the cache. Returns None, when the element does not exist.
        """
        return await self.eval(
            "get_element_data", [self.full_data_cache_key], [element_id]
        )

    @ensure_cache_wrapper()
    async def add_changed_elements(
        self, changed_elements: List[str], deleted_element_ids: List[str]
    ) -> int:
        """
        Modified the full_data_cache to insert the changed_elements and removes the
        deleted_element_ids (in this order). Generates a new change_id and inserts all
        element_ids (changed and deleted) with the change_id into the change_id_cache.
        The newly generated change_id is returned.
        """
        return int(
            await self.eval(
                "add_changed_elements",
                keys=[self.full_data_cache_key, self.change_id_cache_key],
                args=[
                    len(changed_elements),
                    len(deleted_element_ids),
                    *(changed_elements + deleted_element_ids),
                ],
            )
        )

    @ensure_cache_wrapper()
    async def get_data_since(
        self, change_id: int, max_change_id: int = -1
    ) -> Tuple[Dict[str, List[bytes]], List[str]]:
        """
        Returns all elements since a change_id (included) and until the max_change_id (included).

        The returend value is a two element tuple. The first value is a dict the elements where
        the key is the collection_string and the value a list of (json-) encoded elements. The
        second element is a list of element_ids, that have been deleted since the change_id.
        """
        changed_elements: Dict[str, List[bytes]] = defaultdict(list)
        deleted_elements: List[str] = []

        # Convert max_change_id to a string. If its negative, use the string '+inf'
        redis_max_change_id = "+inf" if max_change_id < 0 else str(max_change_id)
        # lua script that returns gets all element_ids from change_id_cache_key
        # and then uses each element_id on full_data or restricted_data.
        # It returns a list where the odd values are the change_id and the
        # even values the element as json. The function wait_make_dict creates
        # a python dict from the returned list.
        elements: Dict[bytes, Optional[bytes]] = await aioredis.util.wait_make_dict(
            self.eval(
                "get_data_since",
                keys=[self.full_data_cache_key, self.change_id_cache_key],
                args=[change_id, redis_max_change_id],
            )
        )

        for element_id, element_json in elements.items():
            if element_id.startswith(b"_config"):
                # Ignore config values from the change_id cache key
                continue
            if element_json is None:
                # The element is not in the cache. It has to be deleted.
                deleted_elements.append(element_id.decode())
            else:
                collection_string, id = split_element_id(element_id)
                changed_elements[collection_string].append(element_json)
        return changed_elements, deleted_elements

    async def set_lock(self, lock_name: str) -> bool:
        """
        Tries to sets a lock.

        Returns True when the lock could be set and False, if it was already set.
        """
        # TODO: Improve lock. See: https://redis.io/topics/distlock
        async with get_connection() as redis:
            return await redis.setnx(f"lock_{lock_name}", 1)

    async def get_lock(self, lock_name: str) -> bool:
        """
        Returns True, when the lock is set. Else False.
        """
        async with get_connection() as redis:
            return await redis.get(f"lock_{lock_name}")

    async def del_lock(self, lock_name: str) -> None:
        """
        Deletes the lock. Does nothing when the lock is not set.
        """
        async with get_connection() as redis:
            await redis.delete(f"lock_{lock_name}")

    @ensure_cache_wrapper()
    async def get_current_change_id(self) -> int:
        """
        Get the highest change_id from redis.
        """
        async with get_connection() as redis:
            value = await redis.zrevrangebyscore(
                self.change_id_cache_key, withscores=True, count=1, offset=0
            )
        # Return the score (second element) of the first (and only) element, if exists.
        if not value:
            raise CacheReset()
        return value[0][1]

    @ensure_cache_wrapper()
    async def get_lowest_change_id(self) -> int:
        """
        Get the lowest change_id from redis.
        """
        async with get_connection() as redis:
            value = await redis.zscore(
                self.change_id_cache_key, "_config:lowest_change_id"
            )
        if not value:
            raise CacheReset()
        return value

    async def get_schema_version(self) -> Optional[SchemaVersion]:
        """ Retrieves the schema version of the cache or None, if not existent """
        async with get_connection() as redis:
            schema_version = await redis.hgetall(self.schema_cache_key)
        if not schema_version:
            return None

        return {
            "migration": int(schema_version[b"migration"].decode()),
            "config": int(schema_version[b"config"].decode()),
            "db": schema_version[b"db"].decode(),
        }

    async def set_schema_version(self, schema_version: SchemaVersion) -> None:
        """ Sets the schema version for this cache. """
        async with get_connection() as redis:
            await redis.hmset_dict(self.schema_cache_key, schema_version)

    async def eval(
        self, script_name: str, keys: List[str] = [], args: List[Any] = []
    ) -> Any:
        """
        Runs a lua script in redis. This wrapper around redis.eval tries to make
        usage of redis script cache. First the hash is send to the server and if
        the script is not present there (NOSCRIPT error) the actual script will be
        send.
        If the script uses the ensure_cache-prefix, the first key must be the full_data
        cache key. This is checked here.
        Also this method incudes the custom "CacheReset" error, which will be raised in
        python, if the lua-script returns a "cache_reset" string as an error response.
        """
        hash = self._script_hashes[script_name]
        if self.scripts[script_name][1] and not keys[0] == self.full_data_cache_key:
            raise ImproperlyConfigured(
                "A script with a ensure_cache prefix must have the full_data cache key as its first key"
            )

        async with get_connection() as redis:
            try:
                return await redis.evalsha(hash, keys, args)
            except aioredis.errors.ReplyError as e:
                if str(e).startswith("NOSCRIPT"):
                    return await self._eval(redis, script_name, keys=keys, args=args)
                elif str(e) == "cache_reset":
                    raise CacheReset()
                else:
                    raise e

    async def _eval(
        self, redis: Any, script_name: str, keys: List[str] = [], args: List[Any] = []
    ) -> Any:
        """ Do a real eval of the script (no hash used here). Catches "cache_reset". """
        try:
            return await redis.eval(self.scripts[script_name][0], keys, args)
        except aioredis.errors.ReplyError as e:
            if str(e) == "cache_reset":
                raise CacheReset()
            else:
                raise e


class MemoryCacheProvider:
    """
    CacheProvider for the ElementCache that uses only the memory.

    See the RedisCacheProvider for a description of the methods.

    This provider supports only one process. It saves the data into the memory.
    When you use different processes they will use diffrent data.

    For this reason, the ensure_cache is not used and the schema version always
    returns an invalid schema to always buold the cache.
    """

    def __init__(self, ensure_cache: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self.set_data_dicts()

    def set_data_dicts(self) -> None:
        self.full_data: Dict[str, str] = {}
        self.change_id_data: Dict[int, Set[str]] = {}
        self.locks: Dict[str, str] = {}
        self.default_change_id: int = -1

    async def ensure_cache(self) -> None:
        pass

    async def clear_cache(self) -> None:
        self.set_data_dicts()

    async def reset_full_cache(
        self, data: Dict[str, str], default_change_id: int
    ) -> None:
        self.change_id_data = {}
        self.full_data = data
        self.default_change_id = default_change_id

    async def data_exists(self) -> bool:
        return bool(self.full_data) and self.default_change_id >= 0

    async def get_all_data(self) -> Dict[bytes, bytes]:
        return str_dict_to_bytes(self.full_data)

    async def get_collection_data(self, collection: str) -> Dict[int, bytes]:
        out = {}
        query = f"{collection}:"
        for element_id, value in self.full_data.items():
            if element_id.startswith(query):
                _, id = split_element_id(element_id)
                out[id] = value.encode()
        return out

    async def get_element_data(self, element_id: str) -> Optional[bytes]:
        value = self.full_data.get(element_id, None)
        return value.encode() if value is not None else None

    async def add_changed_elements(
        self, changed_elements: List[str], deleted_element_ids: List[str]
    ) -> int:
        change_id = await self.get_current_change_id() + 1

        for i in range(0, len(changed_elements), 2):
            element_id = changed_elements[i]
            self.full_data[element_id] = changed_elements[i + 1]

            if change_id in self.change_id_data:
                self.change_id_data[change_id].add(element_id)
            else:
                self.change_id_data[change_id] = {element_id}

        for element_id in deleted_element_ids:
            try:
                del self.full_data[element_id]
            except KeyError:
                pass
            if change_id in self.change_id_data:
                self.change_id_data[change_id].add(element_id)
            else:
                self.change_id_data[change_id] = {element_id}

        return change_id

    async def get_data_since(
        self, change_id: int, max_change_id: int = -1
    ) -> Tuple[Dict[str, List[bytes]], List[str]]:
        changed_elements: Dict[str, List[bytes]] = defaultdict(list)
        deleted_elements: List[str] = []

        all_element_ids: Set[str] = set()
        for data_change_id, element_ids in self.change_id_data.items():
            if data_change_id >= change_id and (
                max_change_id == -1 or data_change_id <= max_change_id
            ):
                all_element_ids.update(element_ids)

        for element_id in all_element_ids:
            element_json = self.full_data.get(element_id, None)
            if element_json is None:
                deleted_elements.append(element_id)
            else:
                collection_string, id = split_element_id(element_id)
                changed_elements[collection_string].append(element_json.encode())
        return changed_elements, deleted_elements

    async def set_lock(self, lock_name: str) -> bool:
        if lock_name in self.locks:
            return False
        self.locks[lock_name] = "1"
        return True

    async def get_lock(self, lock_name: str) -> bool:
        return lock_name in self.locks

    async def del_lock(self, lock_name: str) -> None:
        try:
            del self.locks[lock_name]
        except KeyError:
            pass

    async def get_current_change_id(self) -> int:
        if self.change_id_data:
            return max(self.change_id_data.keys())
        else:
            return await self.get_lowest_change_id()

    async def get_lowest_change_id(self) -> int:
        return self.default_change_id

    async def get_schema_version(self) -> Optional[SchemaVersion]:
        return None

    async def set_schema_version(self, schema_version: SchemaVersion) -> None:
        pass


class Cachable(Protocol):
    """
    A Cachable is an object that returns elements that can be cached.

    It needs at least the methods defined here.
    """

    def get_collection_string(self) -> str:
        """
        Returns the string representing the name of the cachable.
        """

    def get_elements(self) -> List[Dict[str, Any]]:
        """
        Returns all elements of the cachable.
        """

    async def restrict_elements(
        self, user_id: int, elements: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Converts full_data to restricted_data.

        elements can be an empty list, a list with some elements of the cachable or with all
        elements of the cachable.
        """
