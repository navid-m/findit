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
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence
from typing import List, Dict, Tuple


class FileIndexer:
    """Handles file indexing and database operations"""

    def __init__(self, db_path: str = "~/.config/everything-linux/fileindex.db"):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = None
        self.cursor = None
        self.init_database()

    def init_database(self):
        """Initialize SQLite database with optimized schema"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
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
        """Index all files in a given path"""
        indexed_count = 0
        fs_type = self.detect_filesystem(root_path)

        print(f"Indexing {root_path} (filesystem: {fs_type})")

        self.cursor.execute("DELETE FROM files WHERE path LIKE ?", (root_path + "%",))

        batch = []
        batch_size = 1000

        try:
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
                        stat_info = os.stat(full_path)
                        batch.append(
                            (
                                full_path,
                                dirname,
                                "",
                                0,
                                int(stat_info.st_mtime),
                                1,  # is_directory
                                fs_type,
                                int(time.time()),
                            )
                        )
                    except (OSError, PermissionError):
                        continue

                for filename in filenames:
                    full_path = os.path.join(dirpath, filename)
                    try:
                        stat_info = os.stat(full_path)
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
                                                 modified, is_directory, filesystem_type, indexed_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                                batch,
                            )
                            self.conn.commit()
                            batch = []

                            if progress_callback:
                                progress_callback(indexed_count, dirpath)

                    except (OSError, PermissionError):
                        continue

            if batch:
                self.cursor.executemany(
                    """
                    INSERT INTO files (path, filename, extension, size, 
                                     modified, is_directory, filesystem_type, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    batch,
                )
                self.conn.commit()

            self.cursor.execute(
                """
                UPDATE mount_points SET last_indexed = ? WHERE path = ?
            """,
                (int(time.time()), root_path),
            )
            self.conn.commit()

            print(f"Indexed {indexed_count} files from {root_path}")

        except Exception as e:
            print(f"Error during indexing: {e}")
            self.conn.rollback()

        return indexed_count

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

        results = []

        try:
            if regex_mode:
                self.cursor.execute(
                    """
                    SELECT path, filename, size, modified, is_directory, filesystem_type
                    FROM files
                    ORDER BY is_directory DESC, filename
                    LIMIT ?
                """,
                    (max_results * 10,),
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

                if search_path:
                    where_clause = f"path LIKE ? {collate}"
                else:
                    where_clause = f"filename LIKE ? {collate}"

                if file_type == "files":
                    where_clause += " AND is_directory = 0"
                elif file_type == "folders":
                    where_clause += " AND is_directory = 1"

                sql = f"""
                    SELECT path, filename, size, modified, is_directory, filesystem_type
                    FROM files
                    WHERE {where_clause}
                    ORDER BY is_directory DESC, filename
                    LIMIT ?
                """

                self.cursor.execute(sql, (like_query, max_results))
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

    def __init__(self, indexer, paths):
        super().__init__()
        self.indexer = indexer
        self.paths = paths
        self.stop_flag = threading.Event()

    def run(self):
        total_indexed = 0
        for path in self.paths:
            if self.stop_flag.is_set():
                break
            count = self.indexer.index_path(
                path, self.progress_callback, self.stop_flag
            )
            total_indexed += count

        self.finished.emit(total_indexed)

    def progress_callback(self, count, current_path):
        self.progress.emit(count, current_path)

    def stop(self):
        self.stop_flag.set()


class MountPointDialog(QDialog):
    """Dialog for managing mount points"""

    def __init__(self, indexer, parent=None):
        super().__init__(parent)
        self.indexer = indexer
        self.setWindowTitle("Manage Mount Points")
        self.setMinimumSize(700, 500)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        group1 = QGroupBox("Available Mount Points")
        group1_layout = QVBoxLayout()

        self.mount_list = QTableWidget()
        self.mount_list.setColumnCount(5)
        self.mount_list.setHorizontalHeaderLabels(
            ["Device", "Mount Point", "Filesystem", "Status", "Last Indexed"]
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

        group2 = QGroupBox("Add Custom Path")
        group2_layout = QHBoxLayout()

        self.custom_path = QLineEdit()
        self.custom_path.setPlaceholderText("Enter path to index...")
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
        """Load available mount points"""
        self.mount_list.setRowCount(0)

        mount_points = self.indexer.get_mount_points()
        indexed_mounts = {m["path"]: m for m in self.indexer.get_indexed_mount_points()}

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
        """Add selected mount points to index"""
        selected_rows = set(item.row() for item in self.mount_list.selectedItems())

        for row in selected_rows:
            mount_point = self.mount_list.item(row, 1).text()
            fs_type = self.mount_list.item(row, 2).text()
            self.indexer.add_mount_point(mount_point, fs_type)

        if selected_rows:
            QMessageBox.information(
                self,
                "Success",
                f"Added {len(selected_rows)} mount point(s). "
                "Use 'Index > Index All' to start indexing.",
            )
            self.load_mount_points()

    def browse_path(self):
        """Browse for custom path"""
        path = QFileDialog.getExistingDirectory(self, "Select Directory to Index")
        if path:
            self.custom_path.setText(path)

    def add_custom_path(self):
        """Add custom path to index"""
        path = self.custom_path.text().strip()
        if path and os.path.exists(path):
            self.indexer.add_mount_point(path)
            QMessageBox.information(
                self,
                "Success",
                f"Added '{path}'. Use 'Index > Index All' to start indexing.",
            )
            self.custom_path.clear()
            self.load_mount_points()
        else:
            QMessageBox.warning(self, "Error", "Enter a valid path.")


class MainWindow(QMainWindow):
    """Main application window"""

    def __init__(self):
        super().__init__()
        self.indexer = FileIndexer()
        self.index_thread = None
        self.search_delay_timer = QTimer()
        self.search_delay_timer.setSingleShot(True)
        self.search_delay_timer.timeout.connect(self.perform_search)

        self.setWindowTitle("Findit")
        self.setMinimumSize(1000, 600)
        self.init_ui()
        self.update_stats()

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

        search_layout.addWidget(QLabel("Search:"))
        search_layout.addWidget(self.search_input)
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

        main_layout.addWidget(self.results_table)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Ready")
        self.status_bar.addWidget(self.status_label)
        self.stats_label = QLabel()
        self.status_bar.addPermanentWidget(self.stats_label)
        self.search_input.setFocus()

    def create_menus(self):
        """Create menu bar"""
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")

        exit_action = QAction("&Exit", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        index_menu = menubar.addMenu("&Index")

        manage_action = QAction("&Manage Mount Points...", self)
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

        manage_action = QAction("Manage Mounts", self)
        manage_action.triggered.connect(self.show_mount_dialog)
        toolbar.addAction(manage_action)

    def on_search_changed(self):
        """Handle search input changes with delay"""
        self.search_delay_timer.stop()
        self.search_delay_timer.start(300)  # 300ms delay

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

        start_time = time.time()
        results = self.indexer.search(
            query, match_case, regex_mode, max_results, search_path, file_type
        )
        search_time = time.time() - start_time

        self.display_results(results)

        self.status_label.setText(
            f"Found {len(results)} result(s) in {search_time:.3f} seconds"
        )

    def display_results(self, results):
        """Display search results in table"""
        self.results_table.setSortingEnabled(False)
        self.results_table.setRowCount(0)

        for row_data in results:
            path, filename, size, modified, is_directory, filesystem = row_data

            row = self.results_table.rowCount()
            self.results_table.insertRow(row)

            self.results_table.setItem(row, 0, QTableWidgetItem(filename))

            dir_path = os.path.dirname(path)
            self.results_table.setItem(row, 1, QTableWidgetItem(dir_path))

            if is_directory:
                size_str = "<DIR>"
            else:
                size_str = self.format_size(size)
            size_item = QTableWidgetItem(size_str)
            size_item.setData(Qt.ItemDataRole.UserRole, size)
            self.results_table.setItem(row, 2, size_item)

            modified_str = datetime.fromtimestamp(modified).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            modified_item = QTableWidgetItem(modified_str)
            modified_item.setData(Qt.ItemDataRole.UserRole, modified)
            self.results_table.setItem(row, 3, modified_item)

            type_str = "Folder" if is_directory else "File"
            self.results_table.setItem(row, 4, QTableWidgetItem(type_str))
            self.results_table.setItem(row, 5, QTableWidgetItem(filesystem))
            self.results_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, path)

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
        """Show mount point management dialog"""
        dialog = MountPointDialog(self.indexer, self)
        dialog.exec()
        self.update_stats()

    def index_all(self):
        """Start indexing all enabled mount points"""
        mount_points = self.indexer.get_indexed_mount_points()
        enabled_mounts = [m["path"] for m in mount_points if m["enabled"]]

        if not enabled_mounts:
            QMessageBox.information(
                self,
                "No Mount Points",
                "No mount points configured. Add some mount points first.",
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
        self.index_thread.start()

        self.status_label.setText("Indexing in progress...")

    def stop_indexing(self):
        """Stop the indexing process"""
        if self.index_thread and self.index_thread.isRunning():
            self.index_thread.stop()
            self.status_label.setText("Stopping indexing...")

    def on_index_progress(self, count, current_path):
        """Update progress during indexing"""
        self.status_label.setText(f"Indexed {count} files... Current: {current_path}")

    def on_index_finished(self, total_count):
        """Handle indexing completion"""
        self.status_label.setText(f"Indexing complete. Indexed {total_count} files.")
        self.update_stats()
        QMessageBox.information(
            self, "Indexing Complete", f"Successfully indexed {total_count} files."
        )

    def update_stats(self):
        """Update statistics display"""
        stats = self.indexer.get_stats()
        self.stats_label.setText(
            f"Files: {stats['files']:,} | "
            f"Folders: {stats['directories']:,} | "
            f"Total: {self.format_size(stats['total_size'])}"
        )

    def show_about(self):
        """Show about dialog"""
        about_text = """<h2>Findit</h2>
        <p>Fast file search utility for Linux</p>
        <p>Features:</p>
        <ul>
        <li>Lightning-fast file search</li>
        <li>NTFS support via ntfs-3g</li>
        <li>Native Linux filesystem support (ext4, xfs, btrfs, etc.)</li>
        <li>Regex search support</li>
        <li>Real-time search as you type</li>
        </ul>
        <p>Version 1.0</p>
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

    # Set application style
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
