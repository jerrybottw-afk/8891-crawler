"""
8891 中古車爬蟲系統 - Tkinter GUI 優化版
整合 Selenium 自動化爬蟲、即時視覺化資料列表、狀態日誌與 SQLite / CSV 匯出
"""

import os
import re
import time
import queue
import sqlite3
import threading
import webbrowser
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText


# ==============================================================================
# 1. 爬蟲核心模組 (支援 CLI 呼叫與 GUI 模組化複用)
# ==============================================================================

def get_chrome(url=None, headless=True):
    """
    啟動 Chrome 瀏覽器並可選開啟指定網址
    :param url: 目標網址 (若為 None 則僅啟動瀏覽器實例供後續使用)
    :param headless: 是否啟動無頭模式 (背景執行)
    :return: WebDriver 實例 (若失敗則回傳 None)
    """
    chrome = None
    try:
        opts = Options()
        if headless:
            opts.add_argument("--headless=new")  # Selenium 4 現代無頭模式

        # 1080P 固定畫布，避免響應式版面折疊
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # 抹除 WebDriver 特徵指紋
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        chrome = webdriver.Chrome(options=opts)
        if url:
            chrome.get(url)
        return chrome
    except Exception as e:
        print(f"WebDriver 啟動異常: {e}")
        if chrome:
            try:
                chrome.quit()
            except Exception:
                pass
        return None


def get_max_page(url="https://auto.8891.com.tw/", driver=None, headless=True):
    """
    取得 8891 中古車頁面的最大總頁數
    :param url: 目標網址
    :param driver: 現有的 WebDriver 實例 (若無則自動新建並關閉)
    :param headless: 是否使用無頭模式新建 driver
    :return: 最大頁碼 (int) 或 None
    """
    need_close = False
    chrome = driver
    try:
        if not chrome:
            chrome = get_chrome(url, headless=headless)
            need_close = True
        else:
            chrome.get(url)

        if chrome:
            soup = BeautifulSoup(chrome.page_source, "lxml")
            pagination = soup.find("ul", class_="ant-pagination")
            if pagination:
                buttons = pagination.find_all("li")
                if len(buttons) >= 3:
                    page_text = buttons[-3].text.strip()
                    if page_text.isdigit():
                        return int(page_text)
    except Exception as e:
        print(f"取得最大頁數失敗: {e}")
    finally:
        if need_close and chrome:
            try:
                chrome.quit()
            except Exception:
                pass
    return None


def parse_page_html(page_source):
    """
    解析 HTML 原始碼並擷取車輛清單
    :param page_source: HTML 字串
    :return: 包含該頁車輛資料的清單 [[縣市, 標題, 年份, 里程, 價格, 圖片連結, 車輛連結], ...]
    """
    data = []
    try:
        soup = BeautifulSoup(page_source, "lxml")
        result_box = soup.find(id="search-result-box")
        if not result_box:
            return data

        cars = result_box.find_all("a", class_="listItem_row-item__kj_nW")
        for car in cars:
            try:
                # 標題
                title_elem = car.find("span", class_="listItem_ib-it-text__l8bxc")
                title = title_elem.text.strip() if title_elem else "未知車款"

                # 縣市
                county_elems = car.find_all("span", class_="listItem_ib-ii-item__ngmuX")
                county = county_elems[0].text.strip() if county_elems else "未標記"

                # 價格
                price_elem = car.find("span", class_="listItem_ib-price__W_88B")
                price = price_elem.text.strip() if price_elem else "面議"

                # 年份與里程
                icon_spans = car.find_all("span", class_="listItem_ib-icon__uVwM4")
                if len(icon_spans) >= 2:
                    year = icon_spans[0].text.strip()
                    km = icon_spans[1].text.strip()
                elif len(icon_spans) == 1:
                    year = icon_spans[0].text.strip()
                    km = "未知里程"
                else:
                    year = "未知年份"
                    km = "未知里程"

                # 連結
                raw_href = car.get("href", "")
                if raw_href:
                    if raw_href.startswith("http"):
                        link = raw_href
                    else:
                        link = "https://auto.8891.com.tw" + ("/" if not raw_href.startswith("/") else "") + raw_href
                else:
                    link = ""

                # 圖片
                img_tag = car.find("img")
                image_url = img_tag.get("src") if img_tag else ""

                data.append([county, title, year, km, price, image_url, link])
            except Exception:
                continue
    except Exception as e:
        print(f"解析 HTML 異常: {e}")
    return data


def get_car_page(url, driver=None, headless=True):
    """
    解析單一頁面的中古車資料 (相容舊版介面)
    """
    need_close = False
    chrome = driver
    try:
        if not chrome:
            chrome = get_chrome(url, headless=headless)
            need_close = True
        else:
            chrome.get(url)

        if not chrome:
            return None
        return parse_page_html(chrome.page_source)
    except Exception as e:
        print(f"頁面解析異常 [{url}]: {e}")
        return None
    finally:
        if need_close and chrome:
            try:
                chrome.quit()
            except Exception:
                pass


def crawl_car_data(start_page=1, end_page=2, delay_sec=1.5, headless=True,
                   callback_page=None, callback_log=None, stop_event=None):
    """
    批次爬取指定範圍頁碼的 8891 車輛資料 (重用單一 Chrome 實例提高效能)
    :param start_page: 起始頁碼
    :param end_page: 結束頁碼
    :param delay_sec: 每一頁間隔秒數
    :param headless: 是否使用無頭模式
    :param callback_page: 每一頁抓取完成後回呼函式 callback_page(page_num, page_items)
    :param callback_log: 日誌回呼函式 callback_log(msg, level)
    :param stop_event: threading.Event 用於外部通知停止
    :return: 累積的資料清單
    """
    def log(msg, level="INFO"):
        if callback_log:
            callback_log(msg, level)
        else:
            print(f"[{level}] {msg}")

    all_data = []
    chrome = None

    try:
        log(f"啟動 Chrome 瀏覽器 (無頭模式={headless})...", "INFO")
        chrome = get_chrome(headless=headless)
        if not chrome:
            log("❌ WebDriver 啟動失敗，請確認已安裝 Chrome 與相應驅動", "ERROR")
            return all_data

        total_pages = end_page - start_page + 1
        log(f"開始爬取第 {start_page} 頁至第 {end_page} 頁 (共 {total_pages} 頁)...", "INFO")

        for current_page in range(start_page, end_page + 1):
            if stop_event and stop_event.is_set():
                log("⚠️ 收到中斷請求，已停止後續爬取！", "WARN")
                break

            target_url = f"https://auto.8891.com.tw/?page={current_page}"
            log(f"正在採集第 {current_page}/{end_page} 頁: {target_url}", "INFO")

            try:
                chrome.get(target_url)
                # 稍候以確保 JS 內容渲染
                time.sleep(1.0)
                page_data = parse_page_html(chrome.page_source)
            except Exception as pe:
                log(f"採集第 {current_page} 頁失敗: {pe}", "ERROR")
                page_data = []

            all_data.extend(page_data)
            log(f"  └ 第 {current_page} 頁成功採集 {len(page_data)} 筆 (累計 {len(all_data)} 筆)", "SUCCESS")

            if callback_page:
                callback_page(current_page, page_data)

            # 禮貌延遲 (支援即時中止)
            if current_page < end_page:
                if stop_event:
                    if stop_event.wait(delay_sec):
                        log("⚠️ 延遲等待中收到停止信號", "WARN")
                        break
                else:
                    time.sleep(delay_sec)

        log(f"🎉 爬蟲作業結束！共成功採集 {len(all_data)} 筆資料", "SUCCESS")
        return all_data

    except Exception as e:
        log(f"爬取過程中發生非預期錯誤: {e}", "ERROR")
        return all_data
    finally:
        if chrome:
            log("關閉 Chrome 瀏覽器實例...", "INFO")
            try:
                chrome.quit()
            except Exception:
                pass


def save_to_sqlite(datas, db_name="auto8891.db", table_name="car", mode="replace"):
    """
    將資料存入 SQLite 資料庫
    :param datas: 車輛資料清單
    :param db_name: 資料庫檔名
    :param table_name: 資料表名稱
    :param mode: 'replace' 覆蓋 或 'append' 追加
    :return: (bool, message)
    """
    if not datas:
        return False, "無資料可供儲存！"

    try:
        columns = ["縣市", "標題", "年份", "里程", "價格", "圖片連結", "車輛連結"]
        df = pd.DataFrame(datas, columns=columns)

        conn = sqlite3.connect(db_name)
        df.to_sql(table_name, conn, if_exists=mode, index=False)
        conn.close()

        msg = f"已將 {len(df)} 筆資料成功寫入 SQLite 資料庫 ({db_name} -> 數據表: {table_name} [模式: {mode}])"
        return True, msg
    except Exception as e:
        return False, f"寫入 SQLite 資料庫失敗: {e}"


def export_to_csv(datas, file_path):
    """
    將資料匯出為 CSV 檔 (UTF-8 BOM 編碼，Excel 開啟不亂碼)
    :param datas: 車輛資料清單
    :param file_path: 檔案路徑
    :return: (bool, message)
    """
    if not datas:
        return False, "無資料可匯出！"

    try:
        columns = ["縣市", "標題", "年份", "里程", "價格", "圖片連結", "車輛連結"]
        df = pd.DataFrame(datas, columns=columns)
        df.to_csv(file_path, index=False, encoding="utf-8-sig")
        return True, f"已成功匯出 {len(df)} 筆資料至 CSV: {file_path}"
    except Exception as e:
        return False, f"匯出 CSV 失敗: {e}"


# ==============================================================================
# 2. Tkinter GUI 應用程式介面
# ==============================================================================

class Auto8891CrawlerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("8891 中古車爬蟲與資料擷取系統")
        self.root.geometry("1100x740")
        self.root.minsize(920, 620)

        # 全域資料與執行緒控制
        self.all_crawled_data = []      # 完整已爬取資料清單
        self.display_data = []           # 當前顯示於表格的資料清單 (支援即時篩選)
        self.is_running = False          # 是否正在執行爬蟲
        self.stop_event = threading.Event()
        self.max_total_pages = None      # 全站最大頁數快取

        # 設定風格
        self._setup_style()
        # 建立 UI 元件
        self._create_widgets()

        # 啟動時於背景非同步查詢全站最大頁碼
        self.root.after(500, self.async_fetch_max_page)

    def _setup_style(self):
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # 配色與字型
        font_main = ("Microsoft JhengHei UI", 9)
        font_bold = ("Microsoft JhengHei UI", 9, "bold")
        font_title = ("Microsoft JhengHei UI", 11, "bold")

        self.style.configure(".", font=font_main)
        self.style.configure("TLabelframe.Label", font=font_bold, foreground="#1565C0")
        self.style.configure("Treeview.Heading", font=font_bold, background="#E3F2FD", foreground="#0D47A1")
        self.style.configure("Treeview", rowheight=26)
        self.style.map("Treeview", background=[("selected", "#BBDEFB")], foreground=[("selected", "#0D47A1")])

        # 自訂按鈕風格
        self.style.configure("Primary.TButton", font=font_bold, foreground="white", background="#1976D2")
        self.style.map("Primary.TButton", background=[("active", "#1565C0"), ("disabled", "#B0BEC5")])
        self.style.configure("Danger.TButton", font=font_bold, foreground="white", background="#D32F2F")
        self.style.map("Danger.TButton", background=[("active", "#B71C1C"), ("disabled", "#B0BEC5")])

    def _create_widgets(self):
        # 主容器
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. 頂部：標題與輸入控制面板 (優化輸入)
        self._create_input_panel(main_frame)

        # 2. 中間：輸出資料面板 (優化輸出，包含 Notebook 分頁：資料清單與執行日誌)
        self._create_output_panel(main_frame)

        # 3. 底部：進度條與狀態列
        self._create_status_bar(main_frame)

    # --------------------------------------------------------------------------
    # 頂部：輸入與參數設定區
    # --------------------------------------------------------------------------
    def _create_input_panel(self, parent):
        input_labelframe = ttk.LabelFrame(parent, text=" ⚙️ 爬取條件與設定參數 (輸入優化) ", padding=10)
        input_labelframe.pack(fill=tk.X, pady=(0, 10))

        # 第一列：頁碼範圍設定與全站總頁數偵測
        row1 = ttk.Frame(input_labelframe)
        row1.pack(fill=tk.X, pady=3)

        ttk.Label(row1, text="起始頁碼:").pack(side=tk.LEFT, padx=(0, 4))
        self.start_page_var = tk.IntVar(value=1)
        self.spin_start = ttk.Spinbox(row1, from_=1, to=9999, textvariable=self.start_page_var, width=6)
        self.spin_start.pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(row1, text="結束頁碼:").pack(side=tk.LEFT, padx=(0, 4))
        self.end_page_var = tk.IntVar(value=2)
        self.spin_end = ttk.Spinbox(row1, from_=1, to=9999, textvariable=self.end_page_var, width=6)
        self.spin_end.pack(side=tk.LEFT, padx=(0, 10))

        # 全站頁數資訊標籤與偵測按鈕
        self.btn_detect_max = ttk.Button(row1, text="🔍 偵測最大頁數", command=self.async_fetch_max_page)
        self.btn_detect_max.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_fill_max = ttk.Button(row1, text="⚡ 填入全站最大頁", command=self._fill_max_page, state=tk.DISABLED)
        self.btn_fill_max.pack(side=tk.LEFT, padx=(0, 15))

        self.lbl_max_info = ttk.Label(row1, text="全站頁數：偵測中...", foreground="#666666")
        self.lbl_max_info.pack(side=tk.LEFT, padx=(0, 10))

        # 快速預設頁數按鈕
        quick_frame = ttk.Frame(row1)
        quick_frame.pack(side=tk.RIGHT)
        ttk.Label(quick_frame, text="快速範本:").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(quick_frame, text="前1頁 (測試)", width=10, command=lambda: self._set_quick_range(1, 1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(quick_frame, text="前3頁", width=7, command=lambda: self._set_quick_range(1, 3)).pack(side=tk.LEFT, padx=2)
        ttk.Button(quick_frame, text="前5頁", width=7, command=lambda: self._set_quick_range(1, 5)).pack(side=tk.LEFT, padx=2)

        # 第二列：爬蟲選項與資料庫設定
        row2 = ttk.Frame(input_labelframe)
        row2.pack(fill=tk.X, pady=6)

        # 無頭模式
        self.headless_var = tk.BooleanVar(value=True)
        self.chk_headless = ttk.Checkbutton(row2, text="無頭模式 (背景靜音執行)", variable=self.headless_var)
        self.chk_headless.pack(side=tk.LEFT, padx=(0, 15))

        # 每頁延遲秒數
        ttk.Label(row2, text="每頁間隔(秒):").pack(side=tk.LEFT, padx=(0, 4))
        self.delay_var = tk.DoubleVar(value=1.5)
        self.spin_delay = ttk.Spinbox(row2, from_=0.5, to=10.0, increment=0.5, textvariable=self.delay_var, width=5)
        self.spin_delay.pack(side=tk.LEFT, padx=(0, 15))

        # 資料庫檔名
        ttk.Label(row2, text="SQLite 檔案:").pack(side=tk.LEFT, padx=(0, 4))
        self.db_name_var = tk.StringVar(value="auto8891.db")
        self.entry_db = ttk.Entry(row2, textvariable=self.db_name_var, width=13)
        self.entry_db.pack(side=tk.LEFT, padx=(0, 15))

        # 資料表名稱
        ttk.Label(row2, text="資料表:").pack(side=tk.LEFT, padx=(0, 4))
        self.table_name_var = tk.StringVar(value="car")
        self.entry_table = ttk.Entry(row2, textvariable=self.table_name_var, width=8)
        self.entry_table.pack(side=tk.LEFT, padx=(0, 15))

        # 存檔模式
        ttk.Label(row2, text="儲存模式:").pack(side=tk.LEFT, padx=(0, 4))
        self.db_mode_var = tk.StringVar(value="覆蓋 (replace)")
        self.combo_mode = ttk.Combobox(row2, textvariable=self.db_mode_var, values=["覆蓋 (replace)", "追加 (append)"], width=13, state="readonly")
        self.combo_mode.pack(side=tk.LEFT, padx=(0, 10))

        # 第三列：操作動作按鈕群
        row3 = ttk.Frame(input_labelframe)
        row3.pack(fill=tk.X, pady=(4, 0))

        self.btn_start = ttk.Button(row3, text="▶ 開始爬取", style="Primary.TButton", command=self.start_crawling)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8), ipadx=10)

        self.btn_stop = ttk.Button(row3, text="⏹ 停止採集", style="Danger.TButton", command=self.stop_crawling, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 15))

        ttk.Separator(row3, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self.btn_save_db = ttk.Button(row3, text="💾 儲存至資料庫", command=self.save_current_to_db)
        self.btn_save_db.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_export_csv = ttk.Button(row3, text="📊 匯出為 CSV", command=self.export_current_to_csv)
        self.btn_export_csv.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_clear = ttk.Button(row3, text="🗑️ 清空畫面", command=self.clear_all_data)
        self.btn_clear.pack(side=tk.LEFT, padx=(0, 6))

    # --------------------------------------------------------------------------
    # 中間：輸出資料與日誌展示區
    # --------------------------------------------------------------------------
    def _create_output_panel(self, parent):
        # 建立 Notebook 分頁
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # 分頁 1: 視覺化資料清單
        self.tab_table = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_table, text="  📋 擷取結果即時列表  ")
        self._create_table_view(self.tab_table)

        # 分頁 2: 系統執行詳細日誌
        self.tab_logs = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_logs, text="  📝 執行日誌 (Console Log)  ")
        self._create_log_view(self.tab_logs)

    def _create_table_view(self, parent):
        # 上方：即時搜尋過濾與統計資訊列
        top_bar = ttk.Frame(parent, padding=(5, 6))
        top_bar.pack(fill=tk.X)

        ttk.Label(top_bar, text="🔍 本地搜尋過濾:").pack(side=tk.LEFT, padx=(0, 5))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *args: self._apply_filter())
        self.entry_filter = ttk.Entry(top_bar, textvariable=self.filter_var, width=22)
        self.entry_filter.pack(side=tk.LEFT, padx=(0, 5))

        ttk.Button(top_bar, text="清除", width=5, command=lambda: self.filter_var.set("")).pack(side=tk.LEFT, padx=(0, 15))

        # 提示標籤
        ttk.Label(top_bar, text="💡 提示: 雙擊任意行可在瀏覽器開啟該中古車詳情頁", foreground="#555555").pack(side=tk.LEFT)

        # 右側統計資訊
        self.lbl_stats = ttk.Label(top_bar, text="總計: 0 筆 | 均價: - 萬", font=("Microsoft JhengHei UI", 9, "bold"), foreground="#0D47A1")
        self.lbl_stats.pack(side=tk.RIGHT, padx=5)

        # 中間：Treeview 表格
        table_container = ttk.Frame(parent)
        table_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

        columns = ("seq", "county", "title", "year", "km", "price", "link")
        self.tree = ttk.Treeview(table_container, columns=columns, show="headings", selectmode="browse")

        self.tree.heading("seq", text="#", anchor=tk.CENTER)
        self.tree.heading("county", text="縣市", anchor=tk.CENTER)
        self.tree.heading("title", text="車輛標題", anchor=tk.W)
        self.tree.heading("year", text="年份", anchor=tk.CENTER)
        self.tree.heading("km", text="里程", anchor=tk.CENTER)
        self.tree.heading("price", text="價格", anchor=tk.CENTER)
        self.tree.heading("link", text="車輛連結", anchor=tk.W)

        self.tree.column("seq", width=45, minwidth=35, anchor=tk.CENTER)
        self.tree.column("county", width=75, minwidth=60, anchor=tk.CENTER)
        self.tree.column("title", width=380, minwidth=200, anchor=tk.W)
        self.tree.column("year", width=80, minwidth=60, anchor=tk.CENTER)
        self.tree.column("km", width=95, minwidth=70, anchor=tk.CENTER)
        self.tree.column("price", width=85, minwidth=65, anchor=tk.CENTER)
        self.tree.column("link", width=300, minwidth=150, anchor=tk.W)

        # 捲軸
        vsb = ttk.Scrollbar(table_container, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(table_container, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        table_container.grid_rowconfigure(0, weight=1)
        table_container.grid_columnconfigure(0, weight=1)

        # 表格互動事件
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-3>", self._on_tree_right_click)

        # 右鍵選單
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="🌐 在瀏覽器中開啟車輛網頁", command=self._open_selected_in_browser)
        self.context_menu.add_command(label="📋 複製車輛連結", command=self._copy_selected_link)
        self.context_menu.add_command(label="📋 複製車輛標題", command=self._copy_selected_title)

    def _create_log_view(self, parent):
        log_frame = ttk.Frame(parent, padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        tool_bar = ttk.Frame(log_frame)
        tool_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(tool_bar, text="清空日誌", command=self._clear_logs).pack(side=tk.RIGHT)

        self.txt_log = ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9), bg="#1E1E1E", fg="#D4D4D4", insertbackground="white")
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        # 設定日誌顏色標籤
        self.txt_log.tag_config("INFO", foreground="#9CDCFE")
        self.txt_log.tag_config("SUCCESS", foreground="#4EC9B0")
        self.txt_log.tag_config("WARN", foreground="#CE9178")
        self.txt_log.tag_config("ERROR", foreground="#F44747")
        self.txt_log.tag_config("TIME", foreground="#6A9955")

    # --------------------------------------------------------------------------
    # 底部：進度條與狀態列
    # --------------------------------------------------------------------------
    def _create_status_bar(self, parent):
        status_frame = ttk.Frame(parent)
        status_frame.pack(fill=tk.X, pady=(5, 0))

        # 進度條
        self.progressbar = ttk.Progressbar(status_frame, orient=tk.HORIZONTAL, mode="determinate")
        self.progressbar.pack(fill=tk.X, pady=(0, 4))

        bottom_bar = ttk.Frame(status_frame)
        bottom_bar.pack(fill=tk.X)

        self.status_var = tk.StringVar(value="系統就緒，請設定頁碼範圍後點擊「開始爬取」。")
        lbl_status = ttk.Label(bottom_bar, textvariable=self.status_var, font=("Microsoft JhengHei UI", 9))
        lbl_status.pack(side=tk.LEFT)

        self.lbl_time_spent = ttk.Label(bottom_bar, text="", foreground="#666666")
        self.lbl_time_spent.pack(side=tk.RIGHT)

    # --------------------------------------------------------------------------
    # 輔助與 UI 回應邏輯
    # --------------------------------------------------------------------------
    def log(self, message, level="INFO"):
        """將日誌訊息安全地加入至日誌面板 (執行緒安全)"""
        def _append():
            timestamp = datetime.now().strftime("[%H:%M:%S] ")
            self.txt_log.insert(tk.END, timestamp, "TIME")
            self.txt_log.insert(tk.END, f"[{level}] ", level)
            self.txt_log.insert(tk.END, f"{message}\n")
            self.txt_log.see(tk.END)
        self.root.after(0, _append)

    def _clear_logs(self):
        self.txt_log.delete("1.0", tk.END)

    def _set_quick_range(self, start, end):
        self.start_page_var.set(start)
        self.end_page_var.set(end)

    def _fill_max_page(self):
        if self.max_total_pages:
            self.end_page_var.set(self.max_total_pages)

    def async_fetch_max_page(self):
        """背景非同步取得全站最大頁碼"""
        self.lbl_max_info.config(text="全站頁數：正在查詢...", foreground="#E65100")
        self.btn_detect_max.config(state=tk.DISABLED)

        def worker():
            max_p = get_max_page("https://auto.8891.com.tw/", headless=True)
            def update_ui():
                self.btn_detect_max.config(state=tk.NORMAL)
                if max_p:
                    self.max_total_pages = max_p
                    self.lbl_max_info.config(text=f"全站目前最大總頁數：{max_p} 頁", foreground="#2E7D32")
                    self.btn_fill_max.config(state=tk.NORMAL)
                    self.log(f"已成功取得 8891 全站目前最大總頁數: {max_p}", "SUCCESS")
                else:
                    self.lbl_max_info.config(text="全站頁數：查詢逾時或結構變更", foreground="#C62828")
                    self.log("取得全站最大頁數失敗，請手動指定結束頁碼", "WARN")
            self.root.after(0, update_ui)

        threading.Thread(target=worker, daemon=True).start()

    # --------------------------------------------------------------------------
    # 資料表格呈現與本地過濾
    # --------------------------------------------------------------------------
    def _add_items_to_table(self, new_items):
        """爬取過程中即時追加新資料至全域清單並更新畫面"""
        start_seq = len(self.all_crawled_data) + 1
        for idx, item in enumerate(new_items):
            # item 格式: [縣市, 標題, 年份, 里程, 價格, 圖片連結, 車輛連結]
            seq = start_seq + idx
            entry = [seq] + item
            self.all_crawled_data.append(entry)

        self._apply_filter()
        self._update_stats()

    def _apply_filter(self):
        """根據搜尋關鍵字即時過濾顯示結果"""
        kw = self.filter_var.get().strip().lower()
        for row in self.tree.get_children():
            self.tree.delete(row)

        if not kw:
            self.display_data = list(self.all_crawled_data)
        else:
            self.display_data = [
                d for d in self.all_crawled_data
                if kw in str(d[1]).lower() or kw in str(d[2]).lower() or kw in str(d[3]).lower()
            ]

        for item in self.display_data:
            # item: [seq, county, title, year, km, price, image_url, link]
            self.tree.insert("", tk.END, values=(
                item[0], item[1], item[2], item[3], item[4], item[5], item[7]
            ))

    def _update_stats(self):
        """更新筆數與平均車價統計"""
        total = len(self.all_crawled_data)
        if total == 0:
            self.lbl_stats.config(text="總計: 0 筆 | 均價: - 萬")
            return

        prices = []
        for item in self.all_crawled_data:
            price_str = str(item[5])
            digits = re.findall(r"(\d+(?:\.\d+)?)", price_str)
            if digits:
                val = float(digits[0])
                if "萬" in price_str or val < 1000:
                    prices.append(val)
                else:
                    prices.append(val / 10000.0)

        if prices:
            avg_price = sum(prices) / len(prices)
            min_price = min(prices)
            max_price = max(prices)
            self.lbl_stats.config(
                text=f"總計: {total} 筆 | 均價: {avg_price:.1f} 萬 (最低 {min_price:.1f} ~ 最高 {max_price:.1f} 萬)"
            )
        else:
            self.lbl_stats.config(text=f"總計: {total} 筆 | 價格皆為議價/面議")

    def _on_tree_double_click(self, event):
        """雙擊列表開啟車輛連結"""
        self._open_selected_in_browser()

    def _on_tree_right_click(self, event):
        """右鍵快顯選單"""
        item_id = self.tree.identify_row(event.y)
        if item_id:
            self.tree.selection_set(item_id)
            self.context_menu.post(event.x_root, event.y_root)

    def _get_selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0], "values")

    def _open_selected_in_browser(self):
        row = self._get_selected_row()
        if row and len(row) >= 7:
            url = row[6]
            if url and url.startswith("http"):
                webbrowser.open(url)
            else:
                messagebox.showinfo("提示", "此車輛無有效網址！")

    def _copy_selected_link(self):
        row = self._get_selected_row()
        if row and len(row) >= 7:
            url = row[6]
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.status_var.set("已將車輛連結複製至剪貼簿！")

    def _copy_selected_title(self):
        row = self._get_selected_row()
        if row and len(row) >= 3:
            title = row[2]
            self.root.clipboard_clear()
            self.root.clipboard_append(title)
            self.status_var.set("已將車輛標題複製至剪貼簿！")

    def clear_all_data(self):
        if self.is_running:
            messagebox.showwarning("提示", "爬蟲執行中，無法清空資料！")
            return
        self.all_crawled_data.clear()
        self.display_data.clear()
        self.filter_var.set("")
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._update_stats()
        self.progressbar["value"] = 0
        self.status_var.set("已清空列表與統計資訊。")

    # --------------------------------------------------------------------------
    # 爬蟲執行與控制 (多執行緒非同步，介面零凍結)
    # --------------------------------------------------------------------------
    def start_crawling(self):
        if self.is_running:
            return

        # 1. 驗證輸入參數 (輸入優化)
        try:
            start_p = int(self.start_page_var.get())
            end_p = int(self.end_page_var.get())
        except ValueError:
            messagebox.showerror("輸入錯誤", "起始頁碼與結束頁碼必須為整數！")
            return

        if start_p < 1:
            messagebox.showerror("輸入錯誤", "起始頁碼不能小於 1！")
            return
        if end_p < start_p:
            messagebox.showerror("輸入錯誤", "結束頁碼不能小於起始頁碼！")
            return

        try:
            delay = float(self.delay_var.get())
            if delay < 0:
                delay = 1.0
        except ValueError:
            delay = 1.5

        headless = self.headless_var.get()
        db_name = self.db_name_var.get().strip() or "auto8891.db"
        table_name = self.table_name_var.get().strip() or "car"
        mode_text = self.db_mode_var.get()
        db_mode = "replace" if "replace" in mode_text or "覆蓋" in mode_text else "append"

        # 2. 切換 UI 狀態
        self.is_running = True
        self.stop_event.clear()
        self._toggle_ui_state(running=True)

        self.progressbar["value"] = 0
        self.progressbar["maximum"] = end_p - start_p + 1

        total_pages = end_p - start_p + 1
        self.status_var.set(f"🚀 開始執行爬蟲：準備採集第 {start_p} ~ {end_p} 頁 (共 {total_pages} 頁)...")
        self.log(f"=== 啟動爬蟲任務 (頁碼: {start_p} -> {end_p}, 延遲: {delay}s, 無頭: {headless}) ===", "INFO")

        start_time = time.time()

        # 3. 背景執行緒
        def run_thread():
            def on_page_done(page_num, page_items):
                def _ui():
                    self._add_items_to_table(page_items)
                    current_prog = page_num - start_p + 1
                    self.progressbar["value"] = current_prog
                    self.status_var.set(f"正在採集... 第 {current_prog}/{total_pages} 頁完成 (累計 {len(self.all_crawled_data)} 筆)")
                self.root.after(0, _ui)

            crawled = crawl_car_data(
                start_page=start_p,
                end_page=end_p,
                delay_sec=delay,
                headless=headless,
                callback_page=on_page_done,
                callback_log=self.log,
                stop_event=self.stop_event
            )

            cost_seconds = time.time() - start_time

            # 結束後的 UI 更新與資料庫儲存
            def on_finish():
                self.is_running = False
                self._toggle_ui_state(running=False)
                self.lbl_time_spent.config(text=f"總耗時: {cost_seconds:.1f} 秒")

                if crawled:
                    # 自動存入資料庫
                    raw_data_only = [item[1:] for item in self.all_crawled_data]
                    ok, db_msg = save_to_sqlite(raw_data_only, db_name=db_name, table_name=table_name, mode=db_mode)
                    if ok:
                        self.log(f"💾 {db_msg}", "SUCCESS")
                        self.status_var.set(f"✅ 爬取完成！共採集 {len(raw_data_only)} 筆資料，已儲存至 {db_name}")
                    else:
                        self.log(f"❌ {db_msg}", "ERROR")
                        self.status_var.set(f"⚠️ 採集完成，但儲存資料庫失敗: {db_msg}")

                    messagebox.showinfo(
                        "爬取完成",
                        f"🎉 爬蟲任務順利完成！\n\n"
                        f"• 採集頁數: {total_pages} 頁\n"
                        f"• 獲取車輛: {len(raw_data_only)} 筆\n"
                        f"• 總花費時間: {cost_seconds:.1f} 秒\n"
                        f"• 資料庫儲存: {db_name} [{table_name}]"
                    )
                else:
                    self.status_var.set("爬蟲結束，未取得任何車輛資料。")
                    if not self.stop_event.is_set():
                        messagebox.showwarning("提示", "未採集到任何資料，請檢查網路連線或稍後重試。")

            self.root.after(0, on_finish)

        threading.Thread(target=run_thread, daemon=True).start()

    def stop_crawling(self):
        if self.is_running:
            self.stop_event.set()
            self.status_var.set("正在停止採集，等待當前頁面釋放...")
            self.log("⚠️ 使用者點擊停止採集按鈕，發送終止信號...", "WARN")
            self.btn_stop.config(state=tk.DISABLED)

    def _toggle_ui_state(self, running):
        """爬取時鎖定輸入元件，避免重複點擊衝突"""
        state = tk.DISABLED if running else tk.NORMAL
        self.btn_start.config(state=state)
        self.btn_stop.config(state=tk.NORMAL if running else tk.DISABLED)
        self.spin_start.config(state=state)
        self.spin_end.config(state=state)
        self.spin_delay.config(state=state)
        self.chk_headless.config(state=state)
        self.entry_db.config(state=state)
        self.entry_table.config(state=state)
        self.combo_mode.config(state=state)
        self.btn_save_db.config(state=state)
        self.btn_export_csv.config(state=state)
        self.btn_clear.config(state=state)

    # --------------------------------------------------------------------------
    # 匯出與儲存功能 (輸出優化)
    # --------------------------------------------------------------------------
    def save_current_to_db(self):
        """手動將當前表格內容存入 SQLite"""
        if not self.all_crawled_data:
            messagebox.showwarning("提示", "目前尚無任何車輛資料可儲存！")
            return

        db_name = self.db_name_var.get().strip() or "auto8891.db"
        table_name = self.table_name_var.get().strip() or "car"
        mode_text = self.db_mode_var.get()
        db_mode = "replace" if "replace" in mode_text or "覆蓋" in mode_text else "append"

        raw_data = [item[1:] for item in self.all_crawled_data]
        ok, msg = save_to_sqlite(raw_data, db_name=db_name, table_name=table_name, mode=db_mode)
        if ok:
            messagebox.showinfo("成功", f"🎉 {msg}")
            self.log(f"手動儲存: {msg}", "SUCCESS")
        else:
            messagebox.showerror("失敗", f"❌ {msg}")
            self.log(f"手動儲存失敗: {msg}", "ERROR")

    def export_current_to_csv(self):
        """將當前資料匯出為 CSV 檔"""
        if not self.all_crawled_data:
            messagebox.showwarning("提示", "目前尚無任何車輛資料可匯出！")
            return

        default_filename = f"auto8891_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=default_filename,
            filetypes=[("CSV 檔案 (*.csv)", "*.csv"), ("所有檔案 (*.*)", "*.*")],
            title="儲存 CSV 檔案"
        )
        if not file_path:
            return

        raw_data = [item[1:] for item in self.all_crawled_data]
        ok, msg = export_to_csv(raw_data, file_path)
        if ok:
            messagebox.showinfo("成功", f"🎉 {msg}")
            self.log(f"匯出檔案: {msg}", "SUCCESS")
        else:
            messagebox.showerror("失敗", f"❌ {msg}")
            self.log(f"匯出失敗: {msg}", "ERROR")


# ==============================================================================
# 3. 程式進入點
# ==============================================================================

def main():
    root = tk.Tk()
    app = Auto8891CrawlerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
