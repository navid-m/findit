import os
import ctypes

from typing import List, Tuple, Dict
from pathlib import Path

backend_dir = Path(__file__).parent / "backend"
lib_path = backend_dir / "libfindit_backend.so"

if not lib_path.exists():
    raise RuntimeError(
        f"Nim backend library not found at {lib_path}. Run: cd backend && ./build.sh"
    )

lib = ctypes.CDLL(str(lib_path))

lib.initNim()

ProgressCallback = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p)

lib.createIndexer.argtypes = [ctypes.c_char_p]
lib.createIndexer.restype = ctypes.c_void_p
lib.destroyIndexer.argtypes = [ctypes.c_void_p]
lib.destroyIndexer.restype = None
lib.setStopFlag.argtypes = [ctypes.c_void_p, ctypes.c_bool]
lib.setStopFlag.restype = None
lib.detectFilesystem.argtypes = [ctypes.c_char_p]
lib.detectFilesystem.restype = ctypes.c_char_p
lib.addMountPoint.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
lib.addMountPoint.restype = ctypes.c_bool
lib.indexPath.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ProgressCallback]
lib.indexPath.restype = ctypes.c_int

lib.search.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_bool,
    ctypes.c_bool,
    ctypes.c_bool,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)),
    ctypes.POINTER(ctypes.c_int),
]

lib.search.restype = ctypes.c_bool
lib.freeSearchResults.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_int]
lib.freeSearchResults.restype = None

lib.getStats.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_int64),
    ctypes.POINTER(ctypes.c_int64),
    ctypes.POINTER(ctypes.c_int64),
]

lib.getStats.restype = ctypes.c_bool

lib.getIndexedMountPoints.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_int64)),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
    ctypes.POINTER(ctypes.c_int),
]

lib.getIndexedMountPoints.restype = ctypes.c_bool

lib.freeMountPoints.argtypes = [
    ctypes.POINTER(ctypes.c_char_p),
    ctypes.POINTER(ctypes.c_char_p),
    ctypes.POINTER(ctypes.c_int64),
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
]

lib.freeMountPoints.restype = None


class NimFileIndexer:
    """High-performance file indexer using Nim backend"""

    def __init__(self, db_path: str = "~/.config/everything-linux/fileindex.db"):
        self.db_path = os.path.expanduser(db_path)
        self._ctx = lib.createIndexer(self.db_path.encode("utf-8"))
        if not self._ctx:
            raise RuntimeError("Failed to create indexer context")
        self._progress_callback = None

    def __del__(self):
        """Cleanup"""
        if hasattr(self, "_ctx") and self._ctx:
            lib.destroyIndexer(self._ctx)
            self._ctx = None

    def close(self):
        """Close the indexer"""
        if self._ctx:
            lib.destroyIndexer(self._ctx)
            self._ctx = None

    def detect_filesystem(self, path: str) -> str:
        """Detect filesystem type for a path"""
        result = lib.detectFilesystem(path.encode("utf-8"))
        return result.decode("utf-8") if result else "unknown"

    def add_mount_point(self, path: str, fs_type: str = None):
        """Add a mount point to be indexed"""
        if not fs_type:
            fs_type = self.detect_filesystem(path)
        return lib.addMountPoint(
            self._ctx, path.encode("utf-8"), fs_type.encode("utf-8")
        )

    def index_path(self, root_path: str, progress_callback=None, stop_flag=None) -> int:
        """Index all files in a given path"""
        self._progress_callback = None
        if progress_callback:

            @ProgressCallback
            def callback(count, path):
                progress_callback(count, path.decode("utf-8"))

            self._progress_callback = callback
        else:
            self._progress_callback = ProgressCallback(0)

        result = lib.indexPath(
            self._ctx, root_path.encode("utf-8"), self._progress_callback
        )

        return result

    def search(
        self,
        query: str,
        match_case: bool = False,
        regex_mode: bool = False,
        max_results: int = 1000,
        search_path: bool = False,
        file_type: str = "all",
    ) -> List[Tuple]:
        """Search for files matching query"""
        if not query:
            return []

        results_ptr = ctypes.POINTER(ctypes.c_char_p)()
        result_count = ctypes.c_int(0)

        success = lib.search(
            self._ctx,
            query.encode("utf-8"),
            match_case,
            regex_mode,
            search_path,
            file_type.encode("utf-8"),
            max_results,
            ctypes.byref(results_ptr),
            ctypes.byref(result_count),
        )

        if not success:
            return []

        results = []
        if result_count.value > 0:
            for i in range(result_count.value):
                result_str = results_ptr[i].decode("utf-8")
                parts = result_str.split("|")
                if len(parts) == 6:
                    results.append(
                        (
                            parts[0],
                            parts[1],
                            int(parts[2]) if parts[2] else 0,
                            int(parts[3]) if parts[3] else 0,
                            int(parts[4]) if parts[4] else 0,
                            parts[5],
                        )
                    )

            lib.freeSearchResults(results_ptr, result_count.value)

        return results

    def get_stats(self) -> Dict:
        """Get database statistics"""
        file_count = ctypes.c_int64(0)
        dir_count = ctypes.c_int64(0)
        total_size = ctypes.c_int64(0)

        success = lib.getStats(
            self._ctx,
            ctypes.byref(file_count),
            ctypes.byref(dir_count),
            ctypes.byref(total_size),
        )

        if success:
            return {
                "files": file_count.value,
                "directories": dir_count.value,
                "total_size": total_size.value,
            }
        else:
            return {"files": 0, "directories": 0, "total_size": 0}

    def get_indexed_mount_points(self) -> List[Dict]:
        """Get mount points that are tracked in database"""
        paths_ptr = ctypes.POINTER(ctypes.c_char_p)()
        fs_types_ptr = ctypes.POINTER(ctypes.c_char_p)()
        times_ptr = ctypes.POINTER(ctypes.c_int64)()
        enabled_ptr = ctypes.POINTER(ctypes.c_int)()
        count = ctypes.c_int(0)

        success = lib.getIndexedMountPoints(
            self._ctx,
            ctypes.byref(paths_ptr),
            ctypes.byref(fs_types_ptr),
            ctypes.byref(times_ptr),
            ctypes.byref(enabled_ptr),
            ctypes.byref(count),
        )

        results = []
        if success and count.value > 0:
            for i in range(count.value):
                results.append(
                    {
                        "path": paths_ptr[i].decode("utf-8"),
                        "filesystem": fs_types_ptr[i].decode("utf-8"),
                        "last_indexed": times_ptr[i],
                        "enabled": enabled_ptr[i],
                    }
                )

            lib.freeMountPoints(
                paths_ptr, fs_types_ptr, times_ptr, enabled_ptr, count.value
            )

        return results

    def set_stop_flag(self, stop: bool):
        """Set stop flag for indexing"""
        lib.setStopFlag(self._ctx, stop)

    def get_mount_points(self) -> List[Dict]:
        """Get all available mount points (compatible with old interface)"""
        mount_points = []
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        device, mount_point, fs_type = parts[0], parts[1], parts[2]
                        if fs_type in [
                            "ext4",
                            "ext3",
                            "ext2",
                            "xfs",
                            "btrfs",
                            "ntfs",
                            "fuseblk",
                            "ntfs-3g",
                            "vfat",
                            "exfat",
                        ]:
                            if mount_point not in ["/proc", "/sys", "/dev", "/run"]:
                                mount_points.append(
                                    {
                                        "device": device,
                                        "path": mount_point,
                                        "filesystem": fs_type,
                                    }
                                )
        except Exception as e:
            print(f"Error reading mount points: {e}")
        return mount_points

    def init_database(self):
        """Compatibility method - database is initialized in constructor"""
        pass
