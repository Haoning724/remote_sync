import os
import json
import fnmatch
import threading
import time
import stat
import types
from contextlib import contextmanager

import paramiko
from inotify_simple import INotify, flags

# Default file extensions to include when 'source_code_only' is true
SOURCE_CODE_EXTENSIONS = [
    '.py', '.js', '.html', '.css', '.scss', '.java', '.c', '.cpp', '.h',
    '.hpp', '.go', '.rs', '.php', '.rb', '.ts', '.tsx', '.jsx', '.json',
    '.yml', '.yaml', '.md', '.sh', '.xml', '.sql'
]


def is_excluded(path, exclude_patterns, source_code_only=False):
    base_name = os.path.basename(path)
    for pattern in exclude_patterns:
        # Match against the full relative path or just the basename
        path_to_check = path.replace(os.sep, '/')
        if fnmatch.fnmatch(path_to_check, pattern) or fnmatch.fnmatch(base_name, pattern):
            return True
    if source_code_only:
        if os.path.isdir(path):
            return False  # Never exclude directories based on source_code_only
        _, ext = os.path.splitext(base_name)
        if ext.lower() not in SOURCE_CODE_EXTENSIONS:
            return True
    return False


@contextmanager
def sftp_client(config):
    """A context manager for establishing and closing an SFTP connection."""
    ssh_client = None
    sftp = None
    try:
        print(f"[{config['name']}] Connecting to {config['ssh_user']}@{config['ssh_host']}...")
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(
            hostname=config['ssh_host'],
            port=config.get('ssh_port', 22),
            username=config['ssh_user'],
            key_filename=os.path.expanduser(config['ssh_key_path']),
            timeout=15
        )
        sftp = ssh_client.open_sftp()

        if config.get('permissive', False):
            # put
            sftp._original_put = sftp.put

            def put_with_permission(self, local_path, remote_path):
                self._original_put(local_path, remote_path)
                self.chmod(remote_path, 0o777)

            sftp.put = types.MethodType(put_with_permission, sftp)

            # mkdir
            sftp._original_mkdir = sftp.mkdir

            def mkdir_with_permission(self, remote_path):
                self._original_mkdir(remote_path)
                self.chmod(remote_path, 0o777)

            sftp.mkdir = types.MethodType(mkdir_with_permission, sftp)

        print(f"[{config['name']}] SFTP connection established.")
        yield sftp
    except Exception as e:
        print(f"[{config['name']}] SFTP Connection Error: {e}")
        yield None
    finally:
        if sftp: sftp.close()
        if ssh_client: ssh_client.close()
        # print(f"[{config['name']}] SFTP connection closed.") # Less verbose


def walk_remote(sftp, remote_path, exclude_patterns, source_code_only):
    """Recursively walk a remote directory, yielding relative paths and attributes."""
    remote_map = {}

    # Use a stack for iterative traversal instead of pure recursion
    stack = [""]

    while stack:
        current_rel_path = stack.pop()
        current_remote_path = os.path.join(remote_path, current_rel_path).replace("\\", "/")

        try:
            for attr in sftp.listdir_attr(current_remote_path):
                rel_item_path = os.path.join(current_rel_path, attr.filename).replace("\\", "/")
                is_dir = stat.S_ISDIR(attr.st_mode)

                exclude_check_path = rel_item_path + '/' if is_dir else rel_item_path
                if is_excluded(exclude_check_path, exclude_patterns, source_code_only and (not is_dir)):
                    continue

                remote_map[rel_item_path] = attr
                if is_dir:
                    stack.append(rel_item_path)
        except FileNotFoundError:
            # This can happen if a directory was deleted during the scan
            print(f"Warning: Remote path not found during scan: {current_remote_path}")
            pass

    return remote_map


def perform_initial_sync(sftp, config):
    """Compares local and remote directories and syncs them."""
    print(f"[{config['name']}] Starting initial sync...")

    local_base = os.path.expanduser(config['local_path'])
    remote_base = config['remote_path']
    exclude_patterns = config.get('exclude_patterns', [])
    source_code_only = config.get('source_code_only', False)
    allow_delete = config.get('initial_sync', {}).get('delete', False)

    # 1. Build map of local files
    local_map = {}
    for root, dirs, files in os.walk(local_base, topdown=True):
        # Filter directories in-place using the exclude patterns
        dirs[:] = [d for d in dirs if
                   not is_excluded(os.path.relpath(os.path.join(root, d), local_base) + os.sep, exclude_patterns,
                                   False)]

        for name in dirs:
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, local_base)
            local_map[rel_path] = {'is_dir': True}

        for name in files:
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, local_base)
            if not is_excluded(rel_path, exclude_patterns, source_code_only):
                try:
                    stats = os.stat(full_path)
                    local_map[rel_path] = {'mtime': stats.st_mtime, 'size': stats.st_size, 'is_dir': False}
                except FileNotFoundError:
                    continue  # File might have been deleted during the walk

    # 2. Build map of remote files
    remote_map = walk_remote(sftp, remote_base, exclude_patterns, source_code_only)

    # 3. Determine differences
    local_paths = set(local_map.keys())
    remote_paths = set(remote_map.keys())

    to_upload = local_paths - remote_paths
    to_delete = remote_paths - local_paths
    to_check = local_paths.intersection(remote_paths)

    # 4. Execute actions
    # Create directories and upload files
    # Sort to ensure parent directories are created first
    for rel_path in sorted(list(to_upload)):
        local_full_path = os.path.join(local_base, rel_path)
        remote_full_path = os.path.join(remote_base, rel_path).replace("\\", "/")
        if local_map[rel_path]['is_dir']:
            print(f"[{config['name']}] Initial Sync: Creating dir -> {remote_full_path}")
            try:
                sftp.mkdir(remote_full_path)
            except Exception as e:
                print(f"Error creating dir {remote_full_path}: {e}")
        else:
            print(f"[{config['name']}] Initial Sync: Uploading new file -> {remote_full_path}")
            try:
                sftp.put(local_full_path, remote_full_path)
            except Exception as e:
                print(f"Error uploading {remote_full_path}: {e}")

    # Check for modified files
    for rel_path in to_check:
        if local_map[rel_path]['is_dir']: continue  # Skip directories

        local_file_stat = local_map[rel_path]
        remote_file_attr = remote_map[rel_path]

        # Compare modification time (with a 1-second tolerance) and size
        if int(local_file_stat['mtime']) > remote_file_attr.st_mtime + 1 or local_file_stat[
            'size'] != remote_file_attr.st_size:
            local_full_path = os.path.join(local_base, rel_path)
            remote_full_path = os.path.join(remote_base, rel_path).replace("\\", "/")
            print(f"[{config['name']}] Initial Sync: Updating modified file -> {remote_full_path}")
            try:
                sftp.put(local_full_path, remote_full_path)
            except Exception as e:
                print(f"Error updating {remote_full_path}: {e}")

    # Delete extra remote files and directories
    if allow_delete:
        # Sort in reverse to ensure files are deleted before their parent directories
        for rel_path in sorted(list(to_delete), reverse=True):
            remote_full_path = os.path.join(remote_base, rel_path).replace("\\", "/")
            is_dir = stat.S_ISDIR(remote_map[rel_path].st_mode)
            if is_dir:
                print(f"[{config['name']}] Initial Sync: Deleting extra dir -> {remote_full_path}")
                try:
                    sftp.rmdir(remote_full_path)
                except Exception as e:
                    print(f"Error deleting dir {remote_full_path}: {e}")
            else:
                print(f"[{config['name']}] Initial Sync: Deleting extra file -> {remote_full_path}")
                try:
                    sftp.remove(remote_full_path)
                except Exception as e:
                    print(f"Error deleting file {remote_full_path}: {e}")
    else:
        if to_delete:
            print(
                f"[{config['name']}] Initial Sync: Found {len(to_delete)} extra remote item(s). Deletion is disabled in config.")

    print(f"[{config['name']}] Initial sync completed.")


def sync_worker(config):
    RECONNECT_DELAY = 30
    inotify = INotify()
    watch_flags = (flags.CREATE | flags.DELETE | flags.MODIFY | flags.MOVED_TO | flags.MOVED_FROM |
                   flags.DELETE_SELF | flags.MOVE_SELF)
    wd_map = {}

    def add_watch_recursively(path):
        if not os.path.exists(path): return
        rel_path_from_base = os.path.relpath(path, config['local_path'])
        if rel_path_from_base == '.': rel_path_from_base = ''
        if is_excluded(rel_path_from_base + '/', config.get('exclude_patterns', []),
                       (not os.path.isdir(path)) and config.get('source_code_only', False)):
            return
        try:
            wd = inotify.add_watch(path, watch_flags)
            wd_map[wd] = path
            for item in os.listdir(path):
                child_path = os.path.join(path, item)
                if os.path.isdir(child_path):
                    add_watch_recursively(child_path)
        except Exception as e:
            print(f"[{config['name']}] Error adding watch for {path}: {e}")

    local_path = os.path.expanduser(config['local_path'])
    if not os.path.isdir(local_path):
        print(f"[{config['name']}] ERROR: Local path '{local_path}' does not exist. Worker stopped.")
        return

    add_watch_recursively(local_path)

    while True:
        try:
            with sftp_client(config) as sftp:
                if sftp is None:
                    print(f"[{config['name']}] Connection failed. Retrying in {RECONNECT_DELAY} seconds...")
                    time.sleep(RECONNECT_DELAY)
                    continue

                # --- NEW: Call the initial sync function upon successful connection ---
                if config.get('initial_sync', {}).get('enabled', False):
                    perform_initial_sync(sftp, config)
                else:
                    print(f"[{config['name']}] Initial sync is disabled in config. Skipping.")

                print(f"[{config['name']}] Now monitoring for file changes...")
                while True:
                    for event in inotify.read(timeout=1):
                        wd = event.wd
                        if wd not in wd_map: continue
                        parent_dir_path = wd_map[wd]
                        event_path = os.path.join(parent_dir_path, event.name)
                        rel_path = os.path.relpath(event_path, local_path)
                        remote_path = os.path.join(config['remote_path'], rel_path).replace("\\", "/")
                        exclude_path_check = rel_path + ('/' if event.mask & flags.ISDIR else '')
                        if is_excluded(exclude_path_check, config.get('exclude_patterns', []),
                                       config.get('source_code_only', False)):
                            continue
                        if event.mask & (flags.CREATE | flags.MOVED_TO):
                            if event.mask & flags.ISDIR:
                                print(f"[{config['name']}] Event: Creating remote dir -> {remote_path}")
                                sftp.mkdir(remote_path)
                                add_watch_recursively(event_path)
                            else:
                                print(f"[{config['name']}] Event: Uploading file -> {rel_path}")
                                sftp.put(event_path, remote_path)
                        elif event.mask & flags.MODIFY and not event.mask & flags.ISDIR:
                            print(f"[{config['name']}] Event: MODIFIED -> {rel_path}")
                            sftp.put(event_path, remote_path)
                        elif event.mask & (flags.DELETE | flags.MOVED_FROM):
                            if event.mask & flags.ISDIR:
                                print(f"[{config['name']}] Event: Removing remote dir -> {remote_path}")
                                sftp.rmdir(remote_path)
                            else:
                                print(f"[{config['name']}] Event: Removing remote file -> {remote_path}")
                                sftp.remove(remote_path)

        except (paramiko.ssh_exception.SSHException, EOFError, OSError) as e:
            print(f"[{config['name']}] CRITICAL ERROR: Connection lost ({type(e).__name__}: {e}).")
            print(f"[{config['name']}] Attempting to reconnect in {RECONNECT_DELAY} seconds...")
            time.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            print(f"[{config['name']}] Shutting down worker...")
            break
        except Exception as e:
            print(f"[{config['name']}] An unexpected error occurred: {e}. Restarting connection logic...")
            time.sleep(RECONNECT_DELAY)


def main():
    """Loads configurations and starts a sync worker thread for each."""
    try:
        with open('config.json', 'r') as f:
            configs = json.load(f)
    except FileNotFoundError:
        print("Error: config.json not found. Please create it.")
        return
    except json.JSONDecodeError:
        print("Error: Could not decode config.json. Please check its format.")
        return

    threads = []
    for config in configs:
        if config.get('enabled', False):
            thread = threading.Thread(target=sync_worker, args=(config,))
            threads.append(thread)
            thread.start()

    print(f"Started {len(threads)} sync tasks.")

    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        print("\nCtrl+C detected. Shutting down all sync threads.")


if __name__ == '__main__':
    main()
