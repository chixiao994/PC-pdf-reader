import sys
import os
import json
import glob
import argparse
from collections import OrderedDict
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
import fitz  # PyMuPDF
import threading
import queue
import traceback
import time


class PDFRenderThread(threading.Thread):
    """PDF渲染线程，用于异步加载页面"""
    def __init__(self):
        super().__init__()
        self.queue = queue.Queue(maxsize=10)  # 增大队列大小
        self.running = True
        self.daemon = True
        
    def run(self):
        while self.running:
            try:
                task = self.queue.get(timeout=0.05)  # 缩短超时时间
                if task is None:
                    break
                    
                page_num, page, target_size, callback = task
                try:
                    # 获取页面原始尺寸
                    page_rect = page.rect
                    original_width = page_rect.width
                    original_height = page_rect.height
                    
                    # 目标显示尺寸
                    target_width, target_height = target_size
                    
                    # 计算缩放比例（保持长宽比）
                    width_ratio = target_width / original_width
                    height_ratio = target_height / original_height
                    scale = min(width_ratio, height_ratio)
                    
                    # 设置矩阵
                    mat = fitz.Matrix(scale, scale)
                    
                    # 获取页面图像
                    pix = page.get_pixmap(matrix=mat, colorspace="rgb", alpha=False)
                    
                    # 转换为QPixmap
                    img_data = pix.tobytes("png")
                    image = QImage.fromData(img_data, "PNG")
                    pixmap = QPixmap.fromImage(image)
                    
                    # 回调到主线程
                    callback(page_num, pixmap, scale, original_width, original_height)
                    
                except Exception as e:
                    print(f"渲染页面 {page_num} 失败: {e}")
                    callback(page_num, None, 0, 0, 0)
                    
            except queue.Empty:
                continue
            except Exception as e:
                print(f"渲染线程错误: {e}")
                
    def add_task(self, page_num, page, target_size, callback):
        """添加渲染任务"""
        if self.running:
            # 清除旧的相同页面任务
            with self.queue.mutex:
                # 复制队列内容
                items = list(self.queue.queue)
                # 移除相同页面的旧任务
                items = [item for item in items if item[0] != page_num]
                # 清空队列
                self.queue.queue.clear()
                # 重新添加任务
                for item in items:
                    self.queue.put(item)
            
            # 添加新任务
            self.queue.put((page_num, page, target_size, callback))
            
    def stop(self):
        self.running = False
        self.queue.put(None)


class FileListDialog(QDialog):
    """文件列表对话框，支持删除功能"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.setWindowTitle("PDF文件列表")
        self.setGeometry(300, 300, 600, 500)
        
        self.setStyleSheet("""
            QDialog { background-color: white; }
            QListWidget {
                border: 1px solid #ddd;
                border-radius: 5px;
                background-color: #f9f9f9;
            }
            QListWidget::item { padding: 8px; border-bottom: 1px solid #eee; }
            QListWidget::item:selected { background-color: #e0e0e0; }
            QPushButton {
                padding: 8px 16px;
                border: 1px solid #ccc;
                border-radius: 4px;
                background-color: #f0f0f0;
            }
            QPushButton:hover { background-color: #e0e0e0; }
            QPushButton#deleteBtn {
                background-color: #ff6b6b;
                color: white;
                border: 1px solid #ff4757;
            }
            QPushButton#deleteBtn:hover { background-color: #ff4757; }
            QPushButton#deleteAllBtn {
                background-color: #ff6b6b;
                color: white;
                border: 1px solid #ff4757;
            }
            QPushButton#deleteAllBtn:hover { background-color: #ff4757; }
        """)
        
        self.init_ui()
        self.load_file_list()
        
    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        
        # 标题
        title_label = QLabel("PDF文件列表")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 10px;")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)
        
        # 文件列表
        self.file_list_widget = QListWidget()
        self.file_list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        layout.addWidget(self.file_list_widget)
        
        # 按钮布局
        button_layout = QHBoxLayout()
        
        self.open_btn = QPushButton("打开选中文件")
        self.open_btn.clicked.connect(self.open_selected_file)
        button_layout.addWidget(self.open_btn)
        
        self.delete_btn = QPushButton("删除选中文件")
        self.delete_btn.setObjectName("deleteBtn")
        self.delete_btn.clicked.connect(self.delete_selected_files)
        button_layout.addWidget(self.delete_btn)
        
        self.delete_all_btn = QPushButton("删除全部文件")
        self.delete_all_btn.setObjectName("deleteAllBtn")
        self.delete_all_btn.clicked.connect(self.delete_all_files)
        button_layout.addWidget(self.delete_all_btn)
        
        self.refresh_btn = QPushButton("刷新列表")
        self.refresh_btn.clicked.connect(self.refresh_file_list)
        button_layout.addWidget(self.refresh_btn)
        
        layout.addLayout(button_layout)
        
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)
        
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: gray; font-size: 12px; padding: 5px;")
        layout.addWidget(self.status_label)
        
    def load_file_list(self):
        """加载文件列表"""
        self.file_list_widget.clear()
        pdf_files = self.scan_pdf_files()
        
        if not pdf_files:
            self.file_list_widget.addItem("未找到PDF文件")
            self.file_list_widget.item(0).setFlags(Qt.NoItemFlags)
            self.status_label.setText("当前目录没有PDF文件")
            return
        
        for pdf_file in pdf_files:
            file_name = os.path.basename(pdf_file)
            file_size = os.path.getsize(pdf_file) / (1024 * 1024)
            
            file_key = os.path.abspath(pdf_file)
            if file_key in self.parent.reading_positions:
                last_page = self.parent.reading_positions[file_key]
                display_text = f"{file_name} ({file_size:.1f}MB) - 读到第{last_page + 1}页"
            else:
                display_text = f"{file_name} ({file_size:.1f}MB)"
            
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, pdf_file)
            item.setData(Qt.UserRole + 1, file_name)
            self.file_list_widget.addItem(item)
        
        self.status_label.setText(f"找到 {len(pdf_files)} 个PDF文件")
    
    def scan_pdf_files(self):
        """扫描PDF文件"""
        pdf_files = []
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            pdf_files = glob.glob(os.path.join(current_dir, "*.pdf"))
            pdf_files.sort(key=os.path.getmtime, reverse=True)
        except Exception as e:
            print(f"扫描错误: {e}")
        return pdf_files
    
    def open_selected_file(self):
        """打开选中的文件"""
        selected_items = self.file_list_widget.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "提示", "请先选择要打开的文件")
            return
        
        file_path = selected_items[0].data(Qt.UserRole)
        self.parent.load_pdf_file(file_path)
        self.accept()
    
    def delete_selected_files(self):
        """删除选中的文件"""
        selected_items = self.file_list_widget.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "提示", "请先选择要删除的文件")
            return
        
        file_count = len(selected_items)
        confirm_text = f"确定要删除选中的 {file_count} 个文件吗？\n此操作无法撤销！"
        
        reply = QMessageBox.question(self, "确认删除", confirm_text,
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            deleted_count = 0
            failed_files = []
            
            for item in selected_items:
                file_path = item.data(Qt.UserRole)
                file_name = item.data(Qt.UserRole + 1)
                
                try:
                    os.remove(file_path)
                    file_key = os.path.abspath(file_path)
                    if file_key in self.parent.reading_positions:
                        del self.parent.reading_positions[file_key]
                        self.parent.save_reading_positions()
                    
                    deleted_count += 1
                    print(f"已删除文件: {file_name}")
                    
                except Exception as e:
                    failed_files.append(f"{file_name}: {str(e)}")
                    print(f"删除文件失败 {file_name}: {e}")
            
            if failed_files:
                error_msg = f"成功删除 {deleted_count} 个文件，失败 {len(failed_files)} 个:\n"
                error_msg += "\n".join(failed_files)
                QMessageBox.warning(self, "删除结果", error_msg)
            else:
                QMessageBox.information(self, "成功", f"已成功删除 {deleted_count} 个文件")
            
            self.load_file_list()
    
    def delete_all_files(self):
        """删除所有PDF文件"""
        item_count = self.file_list_widget.count()
        if item_count == 0 or (item_count == 1 and "未找到PDF文件" in self.file_list_widget.item(0).text()):
            QMessageBox.warning(self, "提示", "当前没有PDF文件可删除")
            return
        
        confirm_text = "确定要删除当前目录中的所有PDF文件吗？\n此操作无法撤销！"
        
        reply = QMessageBox.question(self, "确认删除全部", confirm_text,
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            deleted_count = 0
            failed_files = []
            
            for i in range(self.file_list_widget.count()):
                item = self.file_list_widget.item(i)
                if item.f
