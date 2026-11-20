import hashlib

def get_file_sha256(path, chunk_size=65536):
    """Calculates SHA-256 for a given file path."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()

def get_bytes_sha256(data: bytes):
    """Calculates SHA-256 for bytes in memory."""
    return hashlib.sha256(data).hexdigest()
