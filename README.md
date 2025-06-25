# Python One-way Sync Script: Remote Slaves to Local Masters

This Python script monitors local file changes and updates them to a remote location. Only minimal testing is done.

Monitoring is performed using inotify, which means it is **Linux-only**. The remote connection uses the SFTP protocol via Paramiko.

## Dependencies
```
pip install inotify_simple paramiko
```

## Run
Before running the code, create a `config.json` file for the syncing options. Below is an example configuration:
```json
[
  {
    "name": "Project Alpha Sync",
    "enabled": true,
    "local_path": "/home/user/projects/alpha",
    "remote_path": "/srv/backup/alpha",
    "ssh_host": "remote.server.com",
    "ssh_user": "myuser",
    "ssh_key_path": "/home/user/.ssh/id_rsa",
    "initial_sync": {
      "enabled": true,
      "delete": true 
    },
    "source_code_only": false,
    "exclude_patterns": [
      "*.log",
      "__pycache__/",
      ".git/",
      "node_modules/"
    ]
  },
  {
    "name": "Project Alpha Sync 2",
    "enabled": true,
    "local_path": "/home/user/projects/alpha2",
    "remote_path": "/srv/backup/alpha2",
    "ssh_host": "remote.server.com",
    "ssh_user": "myuser",
    "ssh_key_path": "/home/user/.ssh/id_rsa",
    "initial_sync": {
      "enabled": false,
      "delete": true 
    },
    "source_code_only": false,
    "exclude_patterns": [
      "*.log",
      "__pycache__/",
      ".git/",
      "node_modules/"
    ]
  }
]
```

Then
```
python realtime_sync.py
```
