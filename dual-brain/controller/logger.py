import datetime
import hashlib
import json
import os
import threading
import fcntl
from typing import Dict, Any, Union

class SafeJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)

def get_last_lines_from_file_object(f, chunk_size=4096):
    f.seek(0, 2)
    file_size = f.tell()
    if file_size == 0:
        return
    
    position = file_size
    buffer = b""
    while position > 0:
        to_read = min(chunk_size, position)
        position -= to_read
        f.seek(position, 0)
        chunk = f.read(to_read)
        buffer = chunk + buffer
        
        lines = buffer.split(b"\n")
        if position > 0:
            buffer = lines[0]
            complete_lines = lines[1:]
            current_offset = position + len(lines[0]) + 1
        else:
            buffer = b""
            complete_lines = lines
            current_offset = 0
            
        line_infos = []
        for line in complete_lines:
            start_offset = current_offset
            end_offset = current_offset + len(line)
            line_infos.append((line, start_offset, end_offset))
            current_offset = end_offset + 1
            
        for line_bytes, start_offset, end_offset in reversed(line_infos):
            yield line_bytes, start_offset, end_offset

class SystemLogger:
    """
    A unified, thread-safe, tamper-evident system logger writing to JSON Lines format.
    Chains logs cryptographically using SHA-256 hashes of previous log lines.
    """
    
    VALID_SOURCES = {"controller", "gui_rpa", "os"}
    INITIAL_HASH = "0000000000000000000000000000000000000000000000000000000000000000"

    def __init__(self, log_file_path: str):
        self.log_file_path = os.path.abspath(log_file_path)
        self.lock = threading.Lock()
        
        # Initialize or resume the chain
        self.last_record = None
        self.seq = 0
        self.prev_hash = self.INITIAL_HASH
        
        # If the file exists and is not empty, recover the state
        if os.path.exists(self.log_file_path) and os.path.getsize(self.log_file_path) > 0:
            self._recover_state()

    def _recover_state(self):
        """
        Reads the last valid log entry to resume sequence number and previous hash.
        """
        try:
            with open(self.log_file_path, "r+b") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    file_size = f.seek(0, 2)
                    if file_size == 0:
                        self.seq = 0
                        self.prev_hash = self.INITIAL_HASH
                        self.last_record = None
                        return
                    
                    last_valid_record = None
                    last_valid_end_offset = None
                    
                    for line_bytes, start_offset, end_offset in get_last_lines_from_file_object(f):
                        line_str = line_bytes.decode("utf-8").strip()
                        if not line_str:
                            continue
                        try:
                            record = json.loads(line_str)
                            if isinstance(record, dict) and "seq" in record and "prev_hash" in record:
                                last_valid_record = record
                                last_valid_end_offset = end_offset
                                break
                        except Exception:
                            continue
                    
                    if last_valid_record is not None:
                        self.seq = last_valid_record.get("seq", 0) + 1
                        canonical_last = json.dumps(last_valid_record, sort_keys=True, cls=SafeJSONEncoder)
                        self.prev_hash = hashlib.sha256(canonical_last.encode("utf-8")).hexdigest()
                        self.last_record = last_valid_record
                        
                        if last_valid_end_offset < file_size:
                            truncate_pos = last_valid_end_offset
                            f.seek(last_valid_end_offset)
                            if f.read(1) == b"\n":
                                truncate_pos = last_valid_end_offset + 1
                            f.seek(truncate_pos)
                            f.truncate()
                    else:
                        self.seq = 0
                        self.prev_hash = self.INITIAL_HASH
                        self.last_record = None
                        f.seek(0)
                        f.truncate()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            raise ValueError(f"Failed to recover logger state from log file: {e}")

    def _get_last_valid_record(self, f):
        file_size = f.seek(0, 2)
        if file_size == 0:
            return None
            
        for line_bytes, start_offset, end_offset in get_last_lines_from_file_object(f):
            line_str = line_bytes.decode("utf-8").strip()
            if not line_str:
                continue
            try:
                record = json.loads(line_str)
                if isinstance(record, dict) and "seq" in record and "prev_hash" in record:
                    return record
            except Exception:
                continue
        return None

    def log(self, source: str, event_type: str, payload: Any) -> Dict[str, Any]:
        """
        Log an event.
        
        :param source: One of "controller", "gui_rpa", "os".
        :param event_type: Descriptive name of the event.
        :param payload: Event details (must be JSON serializable, converted to dict if not).
        """
        if source not in self.VALID_SOURCES:
            raise ValueError(f"Invalid source: '{source}'. Must be one of {self.VALID_SOURCES}")
            
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")

        # Normalize payload to a JSON-compatible type
        if payload is None:
            payload = {}
        elif not isinstance(payload, dict):
            # If payload is not a dict, wrap it
            payload = {"data": payload}

        # Clean/sanitize payload to be JSON serializable
        try:
            payload = json.loads(json.dumps(payload, cls=SafeJSONEncoder))
        except Exception:
            pass

        with self.lock:
            # Create ISO-8601 formatted UTC timestamp
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if timestamp.endswith("+00:00"):
                timestamp = timestamp[:-6] + "Z"

            # Ensure the directory exists
            os.makedirs(os.path.dirname(self.log_file_path), exist_ok=True)
            
            with open(self.log_file_path, "a+b") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    # Retrieve the last valid record from the file to synchronize sequence number and prev_hash
                    last_record = self._get_last_valid_record(f)
                    if last_record is not None:
                        self.seq = last_record.get("seq", 0) + 1
                        canonical_last = json.dumps(last_record, sort_keys=True, cls=SafeJSONEncoder)
                        self.prev_hash = hashlib.sha256(canonical_last.encode("utf-8")).hexdigest()
                        self.last_record = last_record
                    else:
                        self.seq = 0
                        self.prev_hash = self.INITIAL_HASH
                        self.last_record = None

                    record = {
                        "timestamp": timestamp,
                        "source": source,
                        "event_type": event_type,
                        "payload": payload,
                        "seq": self.seq,
                        "prev_hash": self.prev_hash
                    }

                    # Write to file
                    # Canonicalize JSON to ensure deterministic hashes
                    serialized = json.dumps(record, sort_keys=True, cls=SafeJSONEncoder)
                    
                    f.seek(0, 2)
                    f.write(serialized.encode("utf-8") + b"\n")
                    f.flush()
                    os.fsync(f.fileno())
                    
                    # Update state for next log
                    self.last_record = record
                    self.seq += 1
                    self.prev_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                    
                    return record
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def verify_chain(self) -> bool:
        """
        Verifies the cryptographic integrity of the entire log file.
        Returns True if chain is intact, False otherwise.
        """
        with self.lock:
            if not os.path.exists(self.log_file_path):
                return self.prev_hash == self.INITIAL_HASH
                
            try:
                with open(self.log_file_path, "r", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        expected_prev_hash = self.INITIAL_HASH
                        idx = 0
                        
                        for line in f:
                            stripped = line.strip()
                            if not stripped:
                                continue
                            
                            record = json.loads(stripped)
                            
                            # Verify seq
                            if record.get("seq") != idx:
                                return False
                                
                            # Verify prev_hash
                            if record.get("prev_hash") != expected_prev_hash:
                                return False
                                
                            # Compute expected hash for next record
                            canonical = json.dumps(record, sort_keys=True, cls=SafeJSONEncoder)
                            expected_prev_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                            idx += 1
                            
                        # Compare expected_prev_hash with in-memory self.prev_hash
                        if expected_prev_hash != self.prev_hash:
                            return False
                            
                        return True
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                return False
