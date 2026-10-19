"""样本文件的流式读写；压缩格式不改变逻辑内容摘要。"""
import gzip
import hashlib
import io
import json
from pathlib import Path


class BundleReader:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.digest = hashlib.sha256()
        self.counts = {}
        self.hashes = {}

    def records(self, name):
        paths = [p for p in (self.folder/(name+'.jsonl'), self.folder/(name+'.jsonl.gz')) if p.is_file()]
        if len(paths) != 1:
            raise ValueError(f'{name}：必须且只能存在一份 JSONL 或 JSONL.GZ 文件')
        self.digest.update(name.encode()+b'\0')
        digest, count = hashlib.sha256(), 0
        opener = gzip.open if paths[0].suffix == '.gz' else open
        with opener(paths[0], 'rb') as stream:
            while line := stream.readline(65537):
                if len(line) > 65536:
                    raise ValueError(f'{name}：第 {count+1} 行超过 64 KiB')
                self.digest.update(line)
                digest.update(line)
                if not line.strip():
                    continue
                count += 1
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise ValueError(f'{name}：第 {count} 行不是有效 JSON') from exc
                if not isinstance(record, dict):
                    raise ValueError(f'{name}：第 {count} 行必须是对象')
                yield record
        self.counts[name] = count
        self.hashes[name] = digest.hexdigest()


class BundleWriter:
    def __init__(self, folder, name):
        self.path = Path(folder)/(name+'.jsonl.gz')
        # 固定压缩头时间，重复构建的文件也能逐字节复现。
        self.raw = self.path.open('wb')
        self.zipped = gzip.GzipFile(filename='', mode='wb', fileobj=self.raw, compresslevel=1, mtime=0)
        self.stream = io.BufferedWriter(self.zipped, buffer_size=1024*1024)
        self.digest, self.count = hashlib.sha256(), 0
        self.buffer = bytearray()

    def write(self, record):
        self.buffer.extend(json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode()+b'\n')
        self.count += 1
        if len(self.buffer) >= 1024*1024:
            self.flush()

    def flush(self):
        self.digest.update(self.buffer)
        self.stream.write(self.buffer)
        self.buffer.clear()

    def close(self):
        self.flush()
        self.stream.close()
        self.raw.close()
        return {'rows':self.count, 'sha256':self.digest.hexdigest(), 'bytes':self.path.stat().st_size}


def peak_memory_bytes():
    """记录整个进程的峰值驻留内存，不把 PostgreSQL 服务计入生成器。"""
    import os
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
                        *[(name,ctypes.c_size_t) for name in ['PeakWorkingSetSize','WorkingSetSize',
                          'QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage',
                          'QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage']]]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi',use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    import resource
    import sys
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == 'darwin' else value*1024
