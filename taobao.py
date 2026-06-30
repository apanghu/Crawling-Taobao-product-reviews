from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import parse_qs, quote_plus, urlparse
import argparse
import hashlib
import json
import logging
import os
import pickle
import random
import subprocess
import sys
import time


class TaobaoScraperNew:
    DEFAULT_REVIEW_TEXTS = {
        "此用户没有填写评价。",
        "评价方未及时做出评价,系统默认好评!",
        "更多",
    }

    def __init__(
        self,
        driver_path: str,
        user_data_dir: str = r"C:\taobao_bot_profile",
        cookie_file: str = "taobao_cookies.pkl",
    ):
        self.driver = None
        self.driver_path = driver_path
        self.user_data_dir = user_data_dir
        self.cookie_file = cookie_file

    def __enter__(self):
        self.initialize_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def initialize_driver(self):
        options = Options()

        os.makedirs(self.user_data_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={self.user_data_dir}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        self.driver = webdriver.Chrome(
            service=Service(self.driver_path),
            options=options,
        )

    def check_login_status(self) -> bool:
        try:
            self.driver.get("https://www.taobao.com")
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.LINK_TEXT, "我的淘宝"))
            )
            return True
        except Exception as e:
            print(f" 登录状态检查失败: {str(e)}")
            return False

    def manual_login(self):
        print("请按以下步骤操作:")
        print("1. 访问 https://login.taobao.com")
        print("2. 使用手机淘宝扫码完成登录")
        print("3. 登录成功后保持页面不动")

        self.driver.get("https://login.taobao.com")
        input(" 完成登录后按回车继续...")

        if not self.check_login_status():
            raise RuntimeError(" 手动登录验证失败")

        with open(self.cookie_file, "wb") as f:
            pickle.dump(self.driver.get_cookies(), f)
        print(" 登录凭证已保存")

    def load_cookies(self) -> bool:
        if not os.path.exists(self.cookie_file):
            return False

        try:
            with open(self.cookie_file, "rb") as f:
                cookies = pickle.load(f)
                self.driver.delete_all_cookies()
                for cookie in cookies:
                    self.driver.add_cookie(cookie)
            self.driver.refresh()
            print(" 历史 Cookie 加载完成")
            return True
        except Exception as e:
            print(f" Cookie 加载异常: {str(e)}")
            return False

    def ensure_login(self):
        if not self.check_login_status():
            print(" 检测到登录状态失效，尝试恢复...")
            if not self.load_cookies() or not self.check_login_status():
                self.manual_login()

    def _normalize_image_url(self, url: str) -> str:
        if not url:
            return ""

        url = url.strip()
        if not url or url.startswith("data:"):
            return ""
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http://"):
            url = "https://" + url[len("http://") :]

        return url

    def _clean_image_urls(self, urls: list) -> list:
        cleaned = []
        seen = set()

        for url in urls:
            url = self._normalize_image_url(url)
            if not url or url in seen:
                continue

            lowered = url.lower()
            if any(token in lowered for token in ("avatar", "head", "icon", "logo")):
                continue

            seen.add(url)
            cleaned.append(url)

        return cleaned

    def _review_id(self, content: str, image_urls: list) -> str:
        seed = content.strip().lower() + "|" + "|".join(image_urls)
        return hashlib.md5(seed.encode("utf-8")).hexdigest()

    def _get_product_id(self, product_url: str) -> str:
        query = parse_qs(urlparse(product_url).query)
        return (query.get("id") or [""])[0]

    def _safe_filename(self, value: str) -> str:
        value = value.strip() or "unknown"
        return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)

    def _collect_product_info(self, product_url: str) -> dict:
        info = self.driver.execute_script(
            """
            const text = (selector) => {
                const el = document.querySelector(selector);
                return el ? (el.innerText || el.textContent || "").trim() : "";
            };
            const attr = (selector, name) => {
                const el = document.querySelector(selector);
                return el ? (el.getAttribute(name) || "") : "";
            };
            const images = Array.from(document.querySelectorAll("img"))
                .map((img) => img.currentSrc || img.src || img.getAttribute("data-src") || "")
                .filter(Boolean)
                .slice(0, 20);

            return {
                title:
                    text("#tbpcDetail_SkuPanelBody .MainTitle--PiA4nmJz span") ||
                    text("[class*='MainTitle'] span") ||
                    text("h1") ||
                    document.title,
                price:
                    text("[class*='Price']") ||
                    text("[class*='price']") ||
                    text("[class*='PriceText']"),
                shop_name:
                    text("[class*='shopName']") ||
                    text("[class*='ShopName']"),
                page_title: document.title,
                main_image_url: attr("meta[property='og:image']", "content"),
                page_image_urls: images,
            };
            """
        )

        main_image_url = self._normalize_image_url(info.get("main_image_url", ""))
        page_image_urls = self._clean_image_urls(info.get("page_image_urls", []))
        if not main_image_url and page_image_urls:
            main_image_url = page_image_urls[0]

        return {
            "product_id": self._get_product_id(product_url),
            "url": product_url,
            "title": info.get("title", ""),
            "price": info.get("price", ""),
            "shop_name": info.get("shop_name", ""),
            "page_title": info.get("page_title", ""),
            "main_image_url": main_image_url,
            "page_image_urls": page_image_urls,
        }

    def _save_product_name(self, output_dir: str, product_name: str):
        if not product_name:
            print("未找到商品名称")
            return

        product_name_file = os.path.join(output_dir, "product_name.txt")
        with open(product_name_file, "w", encoding="utf-8") as f:
            f.write(product_name)
        print(f" 商品名称已保存到: {product_name_file}")

    def _start_optional_analysis_scripts(self):
        analysis_scripts = [
            r"AIGC\Comparison_of_similar_products_and_external_link_information\AIs\prod_brand&name_analysis.py",
            r"AIGC\Comparison_of_similar_products_and_external_link_information\AIs\prod_name_analysis.py",
        ]
        started_analysis = False

        for script in analysis_scripts:
            if os.path.exists(script):
                subprocess.Popen([sys.executable, script])
                started_analysis = True
            else:
                print(f"[跳过] 分析脚本不存在: {script}")

        if started_analysis:
            print("分析脚本已异步启动")
        else:
            print("[提示] 未找到分析脚本，已跳过商品名分析")

    def _extract_review_from_element(self, comment) -> dict:
        review = self.driver.execute_script(
            """
            const contentEl = arguments[0];
            let root = contentEl;

            for (let i = 0; i < 7 && root.parentElement; i++) {
                const parent = root.parentElement;
                const count = parent.querySelectorAll("[class*='content--uonoOhaz']").length;
                if (count > 1) {
                    break;
                }
                root = parent;
            }

            const imageUrls = Array.from(root.querySelectorAll("img"))
                .map((img) => img.currentSrc || img.src || img.getAttribute("data-src") || img.getAttribute("data-ks-lazyload") || "")
                .filter(Boolean);

            const rawText = (root.innerText || root.textContent || "").trim();

            return {
                content: (contentEl.innerText || contentEl.textContent || "").trim(),
                raw_text: rawText,
                image_urls: imageUrls,
            };
            """,
            comment,
        )

        content = (review.get("content") or "").strip()
        image_urls = self._clean_image_urls(review.get("image_urls") or [])
        review_id = self._review_id(content, image_urls)

        return {
            "review_id": review_id,
            "content": content,
            "raw_text": (review.get("raw_text") or "").strip(),
            "image_urls": image_urls,
            "image_count": len(image_urls),
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _process_comment(self, comment, processed: set) -> tuple:
        try:
            review = self._extract_review_from_element(comment)
            content = review["content"]

            if not content or content in self.DEFAULT_REVIEW_TEXTS:
                return None, False

            if review["review_id"] in processed:
                return None, False

            processed.add(review["review_id"])
            return review, True
        except Exception as e:
            print(f" 处理评论时出错: {str(e)}")
            return None, False

    def _has_comment_limit(self, max_comments: int) -> bool:
        return max_comments is not None and max_comments > 0

    def _collect_comments(self, processed: set, max_comments: int) -> tuple:
        current_comments = self.driver.find_elements(By.XPATH, "//*[contains(@class, 'content--uonoOhaz')]")
        new_reviews = []

        for comment in current_comments:
            review, is_new = self._process_comment(comment, processed)
            if is_new:
                new_reviews.append(review)
                if self._has_comment_limit(max_comments) and len(processed) >= max_comments:
                    return new_reviews, True

        return new_reviews, False

    def smart_scroll(self) -> bool:
        try:
            pre_count = len(self.driver.find_elements(By.XPATH, "//*[contains(@class, 'content--uonoOhaz')]"))

            scroll_info = self.driver.execute_script(
                """
                const preCount = arguments[0];
                const commentSelector = "[class*='content--uonoOhaz']";
                const comments = Array.from(document.querySelectorAll(commentSelector));
                const candidates = Array.from(document.querySelectorAll("body, body *"))
                    .filter((el) => {
                        const style = window.getComputedStyle(el);
                        const overflowY = style.overflowY;
                        const scrollable = el.scrollHeight > el.clientHeight + 30;
                        return scrollable && ["auto", "scroll", "overlay"].includes(overflowY);
                    })
                    .map((el) => ({
                        el,
                        commentCount: el.querySelectorAll(commentSelector).length,
                        scrollHeight: el.scrollHeight,
                        clientHeight: el.clientHeight,
                    }))
                    .filter((item) => item.clientHeight > 120);

                candidates.sort((a, b) => {
                    if (b.commentCount !== a.commentCount) {
                        return b.commentCount - a.commentCount;
                    }
                    return b.scrollHeight - a.scrollHeight;
                });

                let container = candidates.length ? candidates[0].el : null;
                if (!container && comments.length) {
                    container = comments[0].closest("[class*='comments'], [class*='Comments'], [class*='Drawer']") ||
                        comments[0].parentElement;
                }
                if (!container) {
                    container = document.scrollingElement || document.documentElement;
                }

                const before = {
                    top: container.scrollTop,
                    height: container.scrollHeight,
                    clientHeight: container.clientHeight,
                    commentCount: preCount,
                    className: container.className || container.tagName,
                };

                const step = Math.max(container.clientHeight * 1.6, 720);
                container.scrollTop = Math.min(container.scrollTop + step, container.scrollHeight);
                container.dispatchEvent(new Event("scroll", { bubbles: true }));
                window.dispatchEvent(new Event("scroll"));

                return {
                    before,
                    after: {
                        top: container.scrollTop,
                        height: container.scrollHeight,
                        clientHeight: container.clientHeight,
                        commentCount: document.querySelectorAll(commentSelector).length,
                        className: container.className || container.tagName,
                    },
                };
                """
                ,
                pre_count,
            )

            time.sleep(random.uniform(1.4, 2.2))

            post_count = len(self.driver.find_elements(By.XPATH, "//*[contains(@class, 'content--uonoOhaz')]"))
            height_changed = scroll_info["after"]["height"] != scroll_info["before"]["height"]
            top_changed = scroll_info["after"]["top"] != scroll_info["before"]["top"]
            print(
                f" 滚动检测: {pre_count} -> {post_count} 条评论, "
                f"容器: {scroll_info['after']['className']}, "
                f"位置: {scroll_info['before']['top']} -> {scroll_info['after']['top']}"
            )
            return post_count > pre_count or height_changed or top_changed
        except Exception as e:
            print(f" 滚动异常: {str(e)}")
            return False

    def scrape_reviews(
        self,
        output_file: str,
        max_comments: int = 0,
        manual_input: bool = True,
        preset_url: str = "",
    ):
        if manual_input:
            product_url = input(" 请输入商品详情页链接: ").strip()
        else:
            if not preset_url:
                raise ValueError("预设链接不能为空")
            product_url = preset_url
            print(f" 使用预设链接: {product_url}")

        output_dir = os.path.dirname(output_file) or "."
        os.makedirs(output_dir, exist_ok=True)

        self.driver.get(product_url)
        time.sleep(2)

        product_info = self._collect_product_info(product_url)
        product_key = product_info.get("product_id") or hashlib.md5(product_url.encode("utf-8")).hexdigest()
        product_key = self._safe_filename(product_key)
        structured_output_file = os.path.join(output_dir, f"product_{product_key}_reviews_latest.json")
        latest_output_file = os.path.join(output_dir, "product_reviews_latest.json")
        self._save_product_name(output_dir, product_info.get("title", ""))
        self._start_optional_analysis_scripts()

        try:
            review_btn = WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, "//div[contains(text(), '查看全部评价')]"))
            )
            self.driver.execute_script("arguments[0].click();", review_btn)
            time.sleep(1.5)
        except Exception as e:
            raise RuntimeError(f"[错误] 无法打开评价页面: {str(e)}")

        processed = set()
        idle_rounds = 0
        max_idle_rounds = 20
        collected_enough = False
        reviews = []

        with open(output_file, "w", encoding="utf-8") as text_file:
            while idle_rounds < max_idle_rounds and not collected_enough:
                new_reviews, collected_enough = self._collect_comments(processed, max_comments)
                new_added = len(new_reviews)

                if new_reviews:
                    reviews.extend(new_reviews)
                    text_file.write("\n".join(review["content"] for review in new_reviews) + "\n")
                    text_file.flush()

                    self._write_structured_results(
                        [structured_output_file, latest_output_file],
                        product_info,
                        reviews,
                        max_comments,
                    )
                    if self._has_comment_limit(max_comments):
                        print(f"[成功] 新增 {new_added} 条评论 [{len(processed)}/{max_comments}]")
                    else:
                        print(f"[成功] 新增 {new_added} 条评论 [累计 {len(processed)}]")

                if collected_enough:
                    print(f"[完成] 已达到采集上限: {max_comments} 条")
                    break

                scrolled = self.smart_scroll()
                if new_added == 0 and not scrolled:
                    idle_rounds += 1
                elif new_added == 0:
                    idle_rounds += 0.5
                else:
                    idle_rounds = 0

                delay = 0.4 if new_added > 0 else 0.8
                time.sleep(delay)

                if idle_rounds >= max_idle_rounds:
                    print("[完成] 连续多轮没有加载出新评价，停止采集")
                    break

        self._write_structured_results(
            [structured_output_file, latest_output_file],
            product_info,
            reviews,
            max_comments,
        )
        print(f"[保存] 结构化数据已保存到: {structured_output_file}")

    def _write_structured_results(self, output_files: list, product_info: dict, reviews: list, max_comments: int):
        collected_at = time.strftime("%Y-%m-%d %H:%M:%S")
        result = {
            "schema_version": "1.0",
            "collected_at": collected_at,
            "run_id": hashlib.md5(f"{product_info.get('url', '')}|{collected_at}".encode("utf-8")).hexdigest(),
            "target_count": max_comments,
            "review_count": len(reviews),
            "product": product_info,
            "reviews": reviews,
        }

        for output_file in output_files:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

    def remove_default_reviews(self, file_path: str):
        temp_file = file_path + ".tmp"

        try:
            with open(file_path, "r", encoding="utf-8") as infile, open(temp_file, "w", encoding="utf-8") as outfile:
                removed_count = 0
                for line in infile:
                    line = line.strip()
                    if line and line not in self.DEFAULT_REVIEW_TEXTS:
                        outfile.write(line + "\n")
                    else:
                        removed_count += 1

                print(f"[清理] 已删除 {removed_count} 条默认/空评价")

            os.replace(temp_file, file_path)
        except Exception as e:
            print(f"[错误] 处理文件出错: {str(e)}")
            if os.path.exists(temp_file):
                os.remove(temp_file)

    def close(self):
        if self.driver:
            self.driver.quit()
            print("[系统] 浏览器实例已关闭")
            if os.path.exists(self.cookie_file):
                os.remove(self.cookie_file)
                print("[清理] 临时 Cookie 文件已清理")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        filename="taobao_scraper.log",
    )

    parser = argparse.ArgumentParser(description="淘宝商品评价爬取工具")
    parser.add_argument(
        "--manual_input",
        type=lambda x: x.lower() == "true",
        default=True,
        help="是否手动输入链接",
    )
    parser.add_argument("--preset_url", type=str, default="", help="预设商品链接")
    parser.add_argument("--max_comments", type=int, default=0, help="最多采集多少条评论，0 表示不限制")
    parser.add_argument(
        "--output_file",
        type=str,
        default=r"AIGC\Comment_crawling_and_analysis\reviews.txt",
        help="评论文本输出路径",
    )

    args = parser.parse_args()

    driver_path = r"D:\chromedriver-win64\chromedriver.exe"
    if not os.path.exists(driver_path):
        logging.error(f"Chrome驱动路径不存在: {driver_path}")
        raise FileNotFoundError(f"Chrome驱动路径不存在: {driver_path}")

    logging.info(f"启动爬虫，参数: manual_input={args.manual_input}, preset_url={args.preset_url}")

    with TaobaoScraperNew(driver_path=driver_path) as scraper:
        scraper.ensure_login()
        scraper.scrape_reviews(
            output_file=args.output_file,
            max_comments=args.max_comments,
            manual_input=args.manual_input,
            preset_url=args.preset_url,
        )
        scraper.remove_default_reviews(args.output_file)
