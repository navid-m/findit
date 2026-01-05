#!/usr/bin/env python3

"""
Findit - NTFS and generic file search utility for Linux

(C) Navid Momtahen 2025 (GPL-3.0)
"""

import re
import os
import sys
import time
import threading
import sqlite3
import subprocess

from datetime import datetime
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QLabel,
    QStatusBar,
    QMenu,
    QDialog,
    QCheckBox,
    QMessageBox,
    QHeaderView,
    QComboBox,
    QSpinBox,
    QFileDialog,
    QGroupBox,
    QToolBar,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent
from PySide6.QtGui import QAction, QFont, QKeySequence, QShortcut
from typing import List, Dict, Tuple

USE_NIM_BACKEND = False

try:
    from nim_backend import NimFileIndexer

    USE_NIM_BACKEND = True
except Exception as e:
    print(f"Nim backend not available ({e}), using Python implementation")
    USE_NIM_BACKEND = False


class FileIndexer:
    """Handles file indexing and database operations"""

    def __init__(self, db_path: str = "~/.config/findit/fileindex.db"):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = None
        self.cursor = None
        self.init_database()

    def init_database(self):
        """Initialize SQLite database with optimized schema"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.cursor.execute("PRAGMA journal_mode=WAL")
        self.cursor.execute("PRAGMA cache_size=10000")
        self.cursor.execute("PRAGMA temp_store=MEMORY")
        self.cursor.execute("PRAGMA synchronous=NORMAL")

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                extension TEXT,
                size INTEGER,
                modified INTEGER,
                is_directory INTEGER,
                filesystem_type TEXT,
                indexed_at INTEGER
            )
        """)

        self.cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_filename ON files(filename COLLATE NOCASE)
        """)
        self.cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_path ON files(path COLLATE NOCASE)
        """)
        self.cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_extension ON files(extension)
        """)
        self.cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_is_directory ON files(is_directory)
        """)

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS mount_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                filesystem_type TEXT,
                last_indexed INTEGER,
                enabled INTEGER DEFAULT 1
            )
        """)

        self.conn.commit()

    def get_mount_points(self) -> List[Dict]:
        """Get all available mount points"""
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

    def add_mount_point(self, path: str, fs_type: str = None):
        """Add a mount point to be indexed"""
        if not fs_type:
            fs_type = self.detect_filesystem(path)

        try:
            self.cursor.execute(
                """
                INSERT OR REPLACE INTO mount_points (path, filesystem_type, enabled)
                VALUES (?, ?, 1)
            """,
                (path, fs_type),
            )
            self.conn.commit()
        except Exception as e:
            print(f"Error adding mount point: {e}")

    def get_indexed_mount_points(self) -> List[Dict]:
        """Get mount points that are tracked in database"""
        self.cursor.execute("""
            SELECT path, filesystem_type, last_indexed, enabled
            FROM mount_points
        """)
        return [
            {
                "path": row[0],
                "filesystem": row[1],
                "last_indexed": row[2],
                "enabled": row[3],
            }
            for row in self.cursor.fetchall()
        ]

    def detect_filesystem(self, path: str) -> str:
        """Detect filesystem type for a given path"""
        try:
            result = subprocess.run(
                ["df", "-T", path], capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 2:
                return lines[1].split()[1]
        except Exception as e:
            print(f"Error detecting filesystem: {e}")
        return "unknown"

    def index_path(self, root_path: str, progress_callback=None, stop_flag=None):
        """Index all files in a given path with batch processing"""
        indexed_count = 0
        fs_type = self.detect_filesystem(root_path)

        print(f"Indexing {root_path} (filesystem: {fs_type})")

        self.cursor.execute("DELETE FROM files WHERE path LIKE ?", (root_path + "%",))

        batch = []
        batch_size = 5000
        progress_interval = 2000

        try:
            self.cursor.execute("BEGIN TRANSACTION")

            for dirpath, dirnames, filenames in os.walk(root_path):
                if stop_flag and stop_flag.is_set():
                    break

                dirnames[:] = [
                    d
                    for d in dirnames
                    if not d.startswith(".") or d in [".local", ".config"]
                ]

                for dirname in dirnames:
                    full_path = os.path.join(dirpath, dirname)
                    try:
                        stat_info = os.lstat(full_path)
                        batch.append(
                            (
                                full_path,
                                dirname,
                                "",
                                0,
                                int(stat_info.st_mtime),
                                1,
                                fs_type,
                                int(time.time()),
                            )
                        )
                    except (OSError, PermissionError):
                        continue

                for filename in filenames:
                    if stop_flag and stop_flag.is_set():
                        break

                    full_path = os.path.join(dirpath, filename)
                    try:
                        stat_info = os.lstat(full_path)
                        extension = os.path.splitext(filename)[1].lower()

                        batch.append(
                            (
                                full_path,
                                filename,
                                extension,
                                stat_info.st_size,
                                int(stat_info.st_mtime),
                                0,
                                fs_type,
                                int(time.time()),
                            )
                        )

                        indexed_count += 1

                        if len(batch) >= batch_size:
                            self.cursor.executemany(
                                """
                                INSERT INTO files (path, filename, extension, size, 
                                                 modified, is_directory, filesystem_type,
                                                 indexed_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                                batch,
                            )
                            self.cursor.execute("COMMIT")
                            self.cursor.execute("BEGIN TRANSACTION")
                            batch = []

                            if (
                                progress_callback
                                and indexed_count % progress_interval == 0
                            ):
                                progress_callback(indexed_count, dirpath)

                    except (OSError, PermissionError):
                        continue

            if batch and not (stop_flag and stop_flag.is_set()):
                self.cursor.executemany(
                    """
                    INSERT INTO files (path, filename, extension, size, 
                                     modified, is_directory, filesystem_type, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    batch,
                )

            self.cursor.execute("COMMIT")

            if not (stop_flag and stop_flag.is_set()):
                self.cursor.execute(
                    """
                    UPDATE mount_points SET last_indexed = ? WHERE path = ?
                """,
                    (int(time.time()), root_path),
                )
                self.conn.commit()

            if stop_flag and stop_flag.is_set():
                print(
                    f"Indexing stopped. Indexed {indexed_count} files from {root_path}"
                )
            else:
                print(f"Indexed {indexed_count} files from {root_path}")

        except Exception as e:
            print(f"Error during indexing: {e}")
            self.cursor.execute("ROLLBACK")

        return indexed_count

    def search(
        self,
        query: str,
        match_case: bool = False,
        regex_mode: bool = False,
        max_results: int = 1000,
        search_path: bool = False,
        file_type: str = "all",
        drive_filter: str = None,
    ) -> List[Tuple]:
        """Search for files matching query"""
        if not query:
            return []

        results = []

        try:
            if regex_mode:
                where_parts = []
                params = []

                if drive_filter and drive_filter != "All Locations":
                    where_parts.append("path LIKE ?")
                    params.append(f"{drive_filter}%")

                where_clause = (
                    f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
                )

                self.cursor.execute(
                    f"""
                    SELECT path, filename, size, modified, is_directory, filesystem_type
                    FROM files
                    {where_clause}
                    ORDER BY is_directory DESC, filename
                    LIMIT ?
                """,
                    (*params, max_results * 5),
                )

                all_files = self.cursor.fetchall()
                pattern = re.compile(query, 0 if match_case else re.IGNORECASE)

                for row in all_files:
                    search_text = row[0] if search_path else row[1]
                    if pattern.search(search_text):
                        results.append(row)
                        if len(results) >= max_results:
                            break
            else:
                if match_case:
                    like_query = f"%{query}%"
                    collate = ""
                else:
                    like_query = f"%{query}%"
                    collate = "COLLATE NOCASE"

                where_parts = []
                params = [like_query]

                if search_path:
                    where_parts.append(f"path LIKE ? {collate}")
                else:
                    where_parts.append(f"filename LIKE ? {collate}")

                if file_type == "files":
                    where_parts.append("is_directory = 0")
                elif file_type == "folders":
                    where_parts.append("is_directory = 1")

                if drive_filter and drive_filter != "All Locations":
                    where_parts.append("path LIKE ?")
                    params.append(f"{drive_filter}%")

                where_clause = " AND ".join(where_parts)

                sql = f"""
                    SELECT path, filename, size, modified, is_directory, filesystem_type
                    FROM files
                    WHERE {where_clause}
                    ORDER BY is_directory DESC, filename
                    LIMIT ?
                """

                params.append(max_results)
                self.cursor.execute(sql, params)
                results = self.cursor.fetchall()

        except Exception as e:
            print(f"Error during search: {e}")

        return results

    def get_stats(self) -> Dict:
        """Get database statistics"""
        try:
            self.cursor.execute("SELECT COUNT(*) FROM files WHERE is_directory = 0")
            file_count = self.cursor.fetchone()[0]

            self.cursor.execute("SELECT COUNT(*) FROM files WHERE is_directory = 1")
            dir_count = self.cursor.fetchone()[0]

            self.cursor.execute("SELECT SUM(size) FROM files WHERE is_directory = 0")
            total_size = self.cursor.fetchone()[0] or 0

            return {
                "files": file_count,
                "directories": dir_count,
                "total_size": total_size,
            }
        except Exception as e:
            print(f"Error getting stats: {e}")
            return {"files": 0, "directories": 0, "total_size": 0}

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()


class IndexThread(QThread):
    """Thread for indexing files in background"""

    progress = Signal(int, str)
    finished = Signal(int)
    stopped = Signal(int)

    def __init__(self, indexer, paths):
        super().__init__()
        self.indexer = indexer
        self.paths = paths
        self.stop_flag = threading.Event()

    def run(self):
        if hasattr(self.indexer, "set_stop_flag"):
            self.indexer.set_stop_flag(False)

        total_indexed = 0
        for path in self.paths:
            if self.stop_flag.is_set():
                break
            count = self.indexer.index_path(
                path, self.progress_callback, self.stop_flag
            )
            total_indexed += count

        if self.stop_flag.is_set():
            self.stopped.emit(total_indexed)
        else:
            self.finished.emit(total_indexed)

    def progress_callback(self, count, current_path):
        self.progress.emit(count, current_path)

    def stop(self):
        self.stop_flag.set()
        if hasattr(self.indexer, "set_stop_flag"):
            self.indexer.set_stop_flag(True)


class IndexerWindow(QDialog):
    """Dedicated window for indexing operations"""

    def __init__(self, indexer, parent=None):
        super().__init__(parent)
        self.indexer = indexer
        self.index_thread = None
        self.setWindowTitle("File Indexer")
        self.setMinimumSize(800, 600)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        info_label = QLabel(
            "<b>File Indexer</b><br>"
            "Select which drives and folders to index and control the indexing process."
        )
        layout.addWidget(info_label)

        group1 = QGroupBox("Available Drives and Folders")
        group1_layout = QVBoxLayout()

        self.drive_list = QTableWidget()
        self.drive_list.setColumnCount(5)
        self.drive_list.setHorizontalHeaderLabels(
            ["Select", "Path", "Filesystem", "Status", "Last Indexed"]
        )
        self.drive_list.horizontalHeader().setStretchLastSection(True)
        self.drive_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        group1_layout.addWidget(self.drive_list)

        btn_layout = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.load_drives)
        self.btn_add_drive = QPushButton("Add Folder/Drive...")
        self.btn_add_drive.clicked.connect(self.add_drive)

        btn_layout.addWidget(self.btn_refresh)
        btn_layout.addWidget(self.btn_add_drive)
        btn_layout.addStretch()

        group1_layout.addLayout(btn_layout)
        group1.setLayout(group1_layout)
        layout.addWidget(group1)

        group2 = QGroupBox("Indexing Control")
        group2_layout = QVBoxLayout()

        self.progress_label = QLabel("Ready to index")
        group2_layout.addWidget(self.progress_label)

        btn_layout2 = QHBoxLayout()
        self.btn_index_selected = QPushButton("Index Selected")
        self.btn_index_selected.clicked.connect(self.index_selected)
        self.btn_index_all = QPushButton("Index All Enabled")
        self.btn_index_all.clicked.connect(self.index_all_enabled)
        self.btn_stop = QPushButton("Stop Indexing")
        self.btn_stop.clicked.connect(self.stop_indexing)
        self.btn_stop.setEnabled(False)

        btn_layout2.addWidget(self.btn_index_selected)
        btn_layout2.addWidget(self.btn_index_all)
        btn_layout2.addWidget(self.btn_stop)

        group2_layout.addLayout(btn_layout2)
        group2.setLayout(group2_layout)
        layout.addWidget(group2)

        btn_layout3 = QHBoxLayout()
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)

        btn_layout3.addStretch()
        btn_layout3.addWidget(self.btn_close)

        layout.addLayout(btn_layout3)
        self.setLayout(layout)

        self.load_drives()

    def load_drives(self):
        """Load available and indexed drives/folders"""
        self.drive_list.setRowCount(0)

        mount_points = self.indexer.get_mount_points()
        indexed_mounts = {m["path"]: m for m in self.indexer.get_indexed_mount_points()}

        for indexed_path, indexed_info in indexed_mounts.items():
            is_mount = any(m["path"] == indexed_path for m in mount_points)
            if not is_mount:
                mount_points.append(
                    {
                        "device": "Folder",
                        "path": indexed_path,
                        "filesystem": indexed_info["filesystem"] or "folder",
                    }
                )

        for mount in mount_points:
            row = self.drive_list.rowCount()
            self.drive_list.insertRow(row)

            checkbox = QCheckBox()
            if mount["path"] in indexed_mounts:
                checkbox.setChecked(indexed_mounts[mount["path"]]["enabled"] == 1)
            checkbox_widget = QWidget()
            checkbox_layout = QHBoxLayout(checkbox_widget)
            checkbox_layout.addWidget(checkbox)
            checkbox_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            checkbox_layout.setContentsMargins(0, 0, 0, 0)
            self.drive_list.setCellWidget(row, 0, checkbox_widget)

            self.drive_list.setItem(row, 1, QTableWidgetItem(mount["path"]))
            self.drive_list.setItem(row, 2, QTableWidgetItem(mount["filesystem"]))

            if mount["path"] in indexed_mounts:
                indexed = indexed_mounts[mount["path"]]
                status = "Enabled" if indexed["enabled"] else "Disabled"
                last_indexed = (
                    datetime.fromtimestamp(indexed["last_indexed"]).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if indexed["last_indexed"]
                    else "Never"
                )
            else:
                status = "Not indexed"
                last_indexed = "Never"

            self.drive_list.setItem(row, 3, QTableWidgetItem(status))
            self.drive_list.setItem(row, 4, QTableWidgetItem(last_indexed))

        self.drive_list.resizeColumnsToContents()

    def add_drive(self):
        """Add a custom drive/folder path"""
        path = QFileDialog.getExistingDirectory(self, "Select Folder or Drive to Index")
        if path:
            self.indexer.add_mount_point(path)
            QMessageBox.information(
                self, "Success", f"Added '{path}' to indexable locations."
            )
            self.load_drives()

    def get_selected_drives(self):
        """Get list of selected drives"""
        selected = []
        for row in range(self.drive_list.rowCount()):
            checkbox_widget = self.drive_list.cellWidget(row, 0)
            checkbox = checkbox_widget.findChild(QCheckBox)
            if checkbox and checkbox.isChecked():
                mount_point = self.drive_list.item(row, 1).text()
                selected.append(mount_point)
        return selected

    def index_selected(self):
        """Index only selected drives/folders"""
        selected = self.get_selected_drives()
        if not selected:
            QMessageBox.warning(
                self, "No Selection", "Select at least one location to index."
            )
            return

        for drive in selected:
            fs_type = None
            for row in range(self.drive_list.rowCount()):
                if self.drive_list.item(row, 1).text() == drive:
                    fs_type = self.drive_list.item(row, 2).text()
                    break
            self.indexer.add_mount_point(drive, fs_type)

        self.start_indexing(selected)

    def index_all_enabled(self):
        """Index all enabled drives/folders"""
        mount_points = self.indexer.get_indexed_mount_points()
        enabled = [m["path"] for m in mount_points if m["enabled"]]

        if not enabled:
            QMessageBox.information(
                self,
                "No Enabled Locations",
                "No locations are enabled for indexing. Select some locations first.",
            )
            return

        self.start_indexing(enabled)

    def start_indexing(self, paths):
        """Start the indexing thread"""
        if self.index_thread and self.index_thread.isRunning():
            QMessageBox.warning(
                self, "Already Indexing", "Indexing is already in progress."
            )
            return

        self.index_thread = IndexThread(self.indexer, paths)
        self.index_thread.progress.connect(self.on_index_progress)
        self.index_thread.finished.connect(self.on_index_finished)
        self.index_thread.stopped.connect(self.on_index_stopped)
        self.index_thread.start()
        self.btn_index_selected.setEnabled(False)
        self.btn_index_all.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_label.setText("Indexing in progress...")

    def stop_indexing(self):
        """Stop the indexing process"""
        if self.index_thread and self.index_thread.isRunning():
            self.index_thread.stop()
            self.progress_label.setText("Stopping indexing...")

    def on_index_progress(self, count, current_path):
        """Update progress during indexing"""
        display_path = (
            current_path if len(current_path) <= 60 else "..." + current_path[-57:]
        )
        self.progress_label.setText(f"Indexed {count:,} files... {display_path}")

    def on_index_finished(self, total_count):
        """Handle indexing completion"""
        self.progress_label.setText(
            f"Indexing complete! Indexed {total_count:,} files."
        )
        self.btn_index_selected.setEnabled(True)
        self.btn_index_all.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.load_drives()
        QMessageBox.information(
            self, "Indexing Complete", f"Successfully indexed {total_count:,} files."
        )

    def on_index_stopped(self, total_count):
        """Handle indexing being stopped"""
        self.progress_label.setText(f"Indexing stopped. Indexed {total_count:,} files.")
        self.btn_index_selected.setEnabled(True)
        self.btn_index_all.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.load_drives()

    def closeEvent(self, event):
        """Handle window close event"""
        if self.index_thread and self.index_thread.isRunning():
            reply = QMessageBox.question(
                self,
                "Indexing in Progress",
                "Indexing is still running. Stop and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.index_thread.stop()
                self.index_thread.wait()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


class MountPointDialog(QDialog):
    """Dialog for managing mount points and folders"""

    def __init__(self, indexer, parent=None):
        super().__init__(parent)
        self.indexer = indexer
        self.setWindowTitle("Manage Locations")
        self.setMinimumSize(700, 500)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        group1 = QGroupBox("Available Mount Points and Indexed Folders")
        group1_layout = QVBoxLayout()

        self.mount_list = QTableWidget()
        self.mount_list.setColumnCount(5)
        self.mount_list.setHorizontalHeaderLabels(
            ["Device", "Path", "Filesystem", "Status", "Last Indexed"]
        )
        self.mount_list.horizontalHeader().setStretchLastSection(True)
        self.mount_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        group1_layout.addWidget(self.mount_list)

        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("Add Selected")
        self.btn_add.clicked.connect(self.add_selected_mounts)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.load_mount_points)

        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_refresh)
        btn_layout.addStretch()

        group1_layout.addLayout(btn_layout)
        group1.setLayout(group1_layout)
        layout.addWidget(group1)

        group2 = QGroupBox("Add Custom Folder")
        group2_layout = QHBoxLayout()

        self.custom_path = QLineEdit()
        self.custom_path.setPlaceholderText("Enter folder path to index...")
        self.btn_browse = QPushButton("Browse")
        self.btn_browse.clicked.connect(self.browse_path)
        self.btn_add_custom = QPushButton("Add")
        self.btn_add_custom.clicked.connect(self.add_custom_path)

        group2_layout.addWidget(self.custom_path)
        group2_layout.addWidget(self.btn_browse)
        group2_layout.addWidget(self.btn_add_custom)

        group2.setLayout(group2_layout)
        layout.addWidget(group2)

        btn_layout2 = QHBoxLayout()
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)

        btn_layout2.addStretch()
        btn_layout2.addWidget(self.btn_close)

        layout.addLayout(btn_layout2)
        self.setLayout(layout)

        self.load_mount_points()

    def load_mount_points(self):
        """Load available mount points and indexed folders"""
        self.mount_list.setRowCount(0)

        mount_points = self.indexer.get_mount_points()
        indexed_mounts = {m["path"]: m for m in self.indexer.get_indexed_mount_points()}

        for indexed_path, indexed_info in indexed_mounts.items():
            is_mount = any(m["path"] == indexed_path for m in mount_points)
            if not is_mount:
                mount_points.append(
                    {
                        "device": "Folder",
                        "path": indexed_path,
                        "filesystem": indexed_info["filesystem"] or "folder",
                    }
                )

        for mount in mount_points:
            row = self.mount_list.rowCount()
            self.mount_list.insertRow(row)

            self.mount_list.setItem(row, 0, QTableWidgetItem(mount["device"]))
            self.mount_list.setItem(row, 1, QTableWidgetItem(mount["path"]))
            self.mount_list.setItem(row, 2, QTableWidgetItem(mount["filesystem"]))

            if mount["path"] in indexed_mounts:
                indexed = indexed_mounts[mount["path"]]
                status = "Indexed" if indexed["enabled"] else "Disabled"
                last_indexed = (
                    datetime.fromtimestamp(indexed["last_indexed"]).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if indexed["last_indexed"]
                    else "Never"
                )
            else:
                status = "Not indexed"
                last_indexed = "Never"

            self.mount_list.setItem(row, 3, QTableWidgetItem(status))
            self.mount_list.setItem(row, 4, QTableWidgetItem(last_indexed))

        self.mount_list.resizeColumnsToContents()

    def add_selected_mounts(self):
        """Add selected mount points/folders to index"""
        selected_rows = set(item.row() for item in self.mount_list.selectedItems())

        for row in selected_rows:
            mount_point = self.mount_list.item(row, 1).text()
            fs_type = self.mount_list.item(row, 2).text()
            self.indexer.add_mount_point(mount_point, fs_type)

        if selected_rows:
            QMessageBox.information(
                self,
                "Success",
                f"Added {len(selected_rows)} location(s). "
                "Use 'Index > Index All' to start indexing.",
            )
            self.load_mount_points()

    def browse_path(self):
        """Browse for custom folder path"""
        path = QFileDialog.getExistingDirectory(self, "Select Folder to Index")
        if path:
            self.custom_path.setText(path)

    def add_custom_path(self):
        """Add custom folder path to index"""
        path = self.custom_path.text().strip()
        if path and os.path.exists(path):
            if os.path.isdir(path):
                self.indexer.add_mount_point(path)
                QMessageBox.information(
                    self,
                    "Success",
                    f"Added '{path}'. Use 'Index > Index All' to start indexing.",
                )
                self.custom_path.clear()
                self.load_mount_points()
            else:
                QMessageBox.warning(self, "Error", "Path must be a directory/folder.")
        else:
            QMessageBox.warning(self, "Error", "Enter a valid folder path.")


class MainWindow(QMainWindow):
    """Main application window"""

    def __init__(self):
        super().__init__()
        if USE_NIM_BACKEND:
            self.indexer = NimFileIndexer()
        else:
            self.indexer = FileIndexer()
        self.index_thread = None
        self.search_delay_timer = QTimer()
        self.search_delay_timer.setSingleShot(True)
        self.search_delay_timer.timeout.connect(self.perform_search)

        backend_name = "Findit" if USE_NIM_BACKEND else "Findit (Non-Native)"
        self.setWindowTitle(backend_name)
        self.setMinimumSize(1000, 600)
        self.init_ui()

        QTimer.singleShot(100, self.update_stats)

    def init_ui(self):
        """Initialize user interface"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        self.create_menus()
        self.create_toolbar()

        search_layout = QHBoxLayout()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search files and folders...")
        self.search_input.textChanged.connect(self.on_search_changed)
        self.search_input.returnPressed.connect(self.perform_search)

        search_font = QFont()
        search_font.setPointSize(11)
        self.search_input.setFont(search_font)

        self.combo_drive = QComboBox()
        self.combo_drive.addItem("All Locations")
        self.combo_drive.currentIndexChanged.connect(self.on_drive_changed)

        QTimer.singleShot(50, self.update_drive_list)

        search_layout.addWidget(QLabel("Search:"))
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(QLabel("Location:"))
        search_layout.addWidget(self.combo_drive)
        main_layout.addLayout(search_layout)

        options_layout = QHBoxLayout()

        self.check_match_case = QCheckBox("Match case")
        self.check_regex = QCheckBox("Regex")
        self.check_search_path = QCheckBox("Search full path")
        self.combo_file_type = QComboBox()
        self.combo_file_type.addItems(["All", "Files only", "Folders only"])
        self.spin_max_results = QSpinBox()
        self.spin_max_results.setRange(100, 10000)
        self.spin_max_results.setValue(1000)
        self.spin_max_results.setPrefix("Max: ")

        options_layout.addWidget(self.check_match_case)
        options_layout.addWidget(self.check_regex)
        options_layout.addWidget(self.check_search_path)
        options_layout.addWidget(QLabel("Type:"))
        options_layout.addWidget(self.combo_file_type)
        options_layout.addWidget(self.spin_max_results)
        options_layout.addStretch()

        main_layout.addLayout(options_layout)

        self.results_table = QTableWidget()
        self.results_table.setColumnCount(6)
        self.results_table.setHorizontalHeaderLabels(
            ["Name", "Path", "Size", "Modified", "Type", "Filesystem"]
        )

        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)

        self.results_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results_table.setSortingEnabled(True)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.doubleClicked.connect(self.open_file)
        self.results_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results_table.customContextMenuRequested.connect(self.show_context_menu)
        self.results_table.verticalHeader().setDefaultSectionSize(24)
        self.results_table.verticalHeader().setMinimumSectionSize(20)
        self.results_table.installEventFilter(self)

        main_layout.addWidget(self.results_table)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Ready")
        self.status_bar.addWidget(self.status_label)
        self.stats_label = QLabel()
        self.status_bar.addPermanentWidget(self.stats_label)
        self.search_input.setFocus()

        focus_results_shortcut = QShortcut(QKeySequence(";"), self)
        focus_results_shortcut.activated.connect(self.focus_results_table)

    def create_menus(self):
        """Create menu bar"""
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")

        exit_action = QAction("&Exit", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        index_menu = menubar.addMenu("&Index")

        indexer_window_action = QAction("&Indexer Window...", self)
        indexer_window_action.setShortcut(QKeySequence("Ctrl+I"))
        indexer_window_action.triggered.connect(self.show_indexer_window)
        index_menu.addAction(indexer_window_action)

        manage_action = QAction("&Manage Locations...", self)
        manage_action.triggered.connect(self.show_mount_dialog)
        index_menu.addAction(manage_action)

        index_menu.addSeparator()

        index_all_action = QAction("Index &All", self)
        index_all_action.setShortcut(QKeySequence("F5"))
        index_all_action.triggered.connect(self.index_all)
        index_menu.addAction(index_all_action)

        stop_index_action = QAction("&Stop Indexing", self)
        stop_index_action.triggered.connect(self.stop_indexing)
        index_menu.addAction(stop_index_action)

        help_menu = menubar.addMenu("&Help")

        about_action = QAction("&About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def create_toolbar(self):
        """Create toolbar"""
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        refresh_action = QAction("Refresh Index", self)
        refresh_action.triggered.connect(self.index_all)
        toolbar.addAction(refresh_action)

        toolbar.addSeparator()

        manage_action = QAction("Manage Locations", self)
        manage_action.triggered.connect(self.show_mount_dialog)
        toolbar.addAction(manage_action)

    def on_search_changed(self):
        """Handle search input changes with delay"""
        self.search_delay_timer.stop()
        self.search_delay_timer.start(190)

    def on_drive_changed(self):
        """Handle drive selection changes"""
        if self.search_input.text().strip():
            self.perform_search()

    def update_drive_list(self):
        """Update the location dropdown with all indexed locations"""
        current_selection = self.combo_drive.currentText()
        self.combo_drive.clear()
        self.combo_drive.addItem("All Locations")

        indexed_mounts = self.indexer.get_indexed_mount_points()
        for mount in indexed_mounts:
            self.combo_drive.addItem(mount["path"])

        index = self.combo_drive.findText(current_selection)
        if index >= 0:
            self.combo_drive.setCurrentIndex(index)

    def perform_search(self):
        """Perform the actual search"""
        query = self.search_input.text().strip()

        if not query:
            self.results_table.setRowCount(0)
            self.status_label.setText("Ready")
            return

        match_case = self.check_match_case.isChecked()
        regex_mode = self.check_regex.isChecked()
        search_path = self.check_search_path.isChecked()
        max_results = self.spin_max_results.value()

        file_type_map = {"All": "all", "Files only": "files", "Folders only": "folders"}
        file_type = file_type_map[self.combo_file_type.currentText()]

        drive_filter = self.combo_drive.currentText()

        self.status_label.setText("Searching...")

        start_time = time.time()

        if USE_NIM_BACKEND:
            results = self.indexer.search(
                query, match_case, regex_mode, max_results, search_path, file_type
            )
            if drive_filter and drive_filter != "All Locations":
                results = [r for r in results if r[0].startswith(drive_filter)]
            search_time = time.time() - start_time
            self.on_search_complete(results, search_time)
        else:
            results = self.indexer.search(
                query,
                match_case,
                regex_mode,
                max_results,
                search_path,
                file_type,
                drive_filter,
            )
            search_time = time.time() - start_time
            self.on_search_complete(results, search_time)

    def on_search_complete(self, results, search_time):
        """Handle search completion"""
        self.display_results(results)
        self.status_label.setText(
            f"Found {len(results):,} result(s) in {search_time:.3f}s"
        )

    def display_results(self, results):
        """Display search results in table"""
        self.results_table.setSortingEnabled(False)
        self.results_table.setUpdatesEnabled(False)
        self.results_table.setRowCount(0)
        self.results_table.setRowCount(len(results))

        for idx, row_data in enumerate(results):
            path, filename, size, modified, is_directory, filesystem = row_data

            self.results_table.setItem(idx, 0, QTableWidgetItem(filename))

            dir_path = os.path.dirname(path)
            self.results_table.setItem(idx, 1, QTableWidgetItem(dir_path))

            if is_directory:
                size_str = "<DIR>"
            else:
                size_str = self.format_size(size)
            size_item = QTableWidgetItem(size_str)
            size_item.setData(Qt.ItemDataRole.UserRole, size)
            self.results_table.setItem(idx, 2, size_item)

            modified_str = datetime.fromtimestamp(modified).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            modified_item = QTableWidgetItem(modified_str)
            modified_item.setData(Qt.ItemDataRole.UserRole, modified)
            self.results_table.setItem(idx, 3, modified_item)

            type_str = "Folder" if is_directory else "File"
            self.results_table.setItem(idx, 4, QTableWidgetItem(type_str))
            self.results_table.setItem(idx, 5, QTableWidgetItem(filesystem))
            self.results_table.item(idx, 0).setData(Qt.ItemDataRole.UserRole, path)

        self.results_table.setUpdatesEnabled(True)
        self.results_table.setSortingEnabled(True)
        self.results_table.resizeColumnToContents(0)

    def format_size(self, size):
        """Format file size in human-readable format"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"

    def open_file(self, index):
        """Open file or folder"""
        row = index.row()
        path = self.results_table.item(row, 0).data(Qt.ItemDataRole.UserRole)

        try:
            if os.path.isdir(path):
                subprocess.Popen(["xdg-open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not open file: {e}")

    def show_context_menu(self, position):
        """Show context menu for results"""
        if self.results_table.selectedItems():
            menu = QMenu()

            open_action = menu.addAction("Open")
            open_action.triggered.connect(
                lambda: self.open_file(self.results_table.currentIndex())
            )

            open_folder_action = menu.addAction("Open Containing Folder")
            open_folder_action.triggered.connect(self.open_containing_folder)

            menu.addSeparator()

            copy_path_action = menu.addAction("Copy Full Path")
            copy_path_action.triggered.connect(self.copy_path)
            copy_name_action = menu.addAction("Copy Name")
            copy_name_action.triggered.connect(self.copy_name)

            menu.exec(self.results_table.viewport().mapToGlobal(position))

    def open_containing_folder(self):
        """Open the folder containing the selected file"""
        row = self.results_table.currentRow()
        if row >= 0:
            path = self.results_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            folder = os.path.dirname(path)
            try:
                subprocess.Popen(["xdg-open", folder])
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Could not open folder: {e}")

    def copy_path(self):
        """Copy full path to clipboard"""
        row = self.results_table.currentRow()
        if row >= 0:
            path = self.results_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            QApplication.clipboard().setText(path)

    def copy_name(self):
        """Copy filename to clipboard"""
        row = self.results_table.currentRow()
        if row >= 0:
            name = self.results_table.item(row, 0).text()
            QApplication.clipboard().setText(name)

    def show_mount_dialog(self):
        """Show location management dialog"""
        dialog = MountPointDialog(self.indexer, self)
        dialog.exec()
        self.update_stats()
        self.update_drive_list()

    def show_indexer_window(self):
        """Show dedicated indexer window"""
        dialog = IndexerWindow(self.indexer, self)
        dialog.exec()
        self.update_stats()
        self.update_drive_list()

    def index_all(self):
        """Start indexing all enabled locations"""
        mount_points = self.indexer.get_indexed_mount_points()
        enabled_mounts = [m["path"] for m in mount_points if m["enabled"]]

        if not enabled_mounts:
            QMessageBox.information(
                self,
                "No Locations",
                "No locations configured. Add some folders or drives first.",
            )
            self.show_mount_dialog()
            return

        if self.index_thread and self.index_thread.isRunning():
            QMessageBox.warning(
                self, "Already Indexing", "Indexing is already in progress."
            )
            return

        self.index_thread = IndexThread(self.indexer, enabled_mounts)
        self.index_thread.progress.connect(self.on_index_progress)
        self.index_thread.finished.connect(self.on_index_finished)
        self.index_thread.stopped.connect(self.on_index_stopped)
        self.index_thread.start()

        self.status_label.setText("Indexing in progress...")

    def stop_indexing(self):
        """Stop the indexing process"""
        if self.index_thread and self.index_thread.isRunning():
            self.index_thread.stop()
            self.status_label.setText("Stopping indexing...")

    def on_index_progress(self, count, current_path):
        """Update progress during indexing"""
        display_path = (
            current_path if len(current_path) <= 50 else "..." + current_path[-47:]
        )
        self.status_label.setText(f"Indexed {count:,} files... {display_path}")

    def on_index_finished(self, total_count):
        """Handle indexing completion"""
        self.status_label.setText(f"Indexing complete. Indexed {total_count:,} files.")
        self.update_stats()
        QMessageBox.information(
            self, "Indexing Complete", f"Successfully indexed {total_count:,} files."
        )

    def on_index_stopped(self, total_count):
        """Handle indexing being stopped"""
        self.status_label.setText(f"Indexing stopped. Indexed {total_count:,} files.")
        self.update_stats()

    def update_stats(self):
        """Update statistics display"""
        stats = self.indexer.get_stats()
        self.stats_label.setText(
            f"Files: {stats['files']:,} | "
            f"Folders: {stats['directories']:,} | "
            f"Total: {self.format_size(stats['total_size'])}"
        )

    def focus_results_table(self):
        """Focus on the results table"""
        if self.results_table.rowCount() > 0:
            self.results_table.setFocus()
            if self.results_table.currentRow() < 0:
                self.results_table.selectRow(0)

    def eventFilter(self, obj, event):
        """Handle key events for the results table"""
        if obj == self.results_table and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
                if self.results_table.currentRow() >= 0:
                    self.open_file(self.results_table.currentIndex())
                    return True
        return super().eventFilter(obj, event)

    def show_about(self):
        """Show about dialog"""
        about_text = """<h2>Findit</h2>
        <p>Author: Navid Momtahen</p>
        <p>License: GPL-3.0</p>
        <p>Fast file search utility for Linux</p>
        <p>Features:</p>
        <ul>
        <li>Lightning-fast file search</li>
        <li>Index entire drives or specific folders</li>
        <li>NTFS support via ntfs-3g</li>
        <li>Native Linux filesystem support (ext4, xfs, btrfs, etc.)</li>
        <li>Regex search support</li>
        <li>Real-time search as you type</li>
        </ul>
        <p>Version 1.0.0</p>
        """
        QMessageBox.about(self, "Findit - About", about_text)

    def closeEvent(self, event):
        """Handle window close event"""
        if self.index_thread and self.index_thread.isRunning():
            reply = QMessageBox.question(
                self,
                "Indexing in Progress",
                "Indexing is still in progress. Do you want to stop and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.index_thread.stop()
                self.index_thread.wait()
            else:
                event.ignore()
                return

        self.indexer.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
