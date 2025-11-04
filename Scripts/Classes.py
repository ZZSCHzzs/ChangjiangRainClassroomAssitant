import requests
import threading
import time
import websocket
import json
from Scripts.Utils import get_user_info, dict_result

wss_url = "wss://changjiang.yuketang.cn/wsapp/"
class Lesson:
    def __init__(self,lessonid,lessonname,classroomid,main_ui):
        self.classroomid = classroomid
        self.lessonid = lessonid
        self.lessonname = lessonname
        self.sessionid = main_ui.config["sessionid"]
        self.headers = {
            "Cookie":"sessionid=%s" % self.sessionid,
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:97.0) Gecko/20100101 Firefox/97.0",
        }
        self.receive_danmu = {}
        self.sent_danmu_dict = {}
        self.danmu_dict = {}
        self.unlocked_problem = []
        self.problem_cache = {}
        self.problem_page_map = {}
        self.ppt_problem_pages = {}
        self.current_presentation_page = {}
        self.notified_problems = set()
        self.auto_answer_warned = False
        self.debug_mode = bool(main_ui.config.get("debug_mode", False))
        self._seen_content_types = set()
        self._seen_answers_types = set()
        self.classmates_ls = []
        self.add_message = main_ui.add_message_signal.emit
        self.add_course = main_ui.add_course_signal.emit
        self.del_course = main_ui.del_course_signal.emit
        self.config = main_ui.config
        code, rtn = get_user_info(self.sessionid)
        self.user_uid = rtn["id"]
        self.user_uname = rtn["name"]
        self.main_ui = main_ui

    def _get_ppt(self,presentationid):
        # 获取课程各页ppt
        r = requests.get(url="https://changjiang.yuketang.cn/api/v3/lesson/presentation/fetch?presentation_id=%s" % (presentationid),headers=self.headers,proxies={"http": None,"https":None})
        return dict_result(r.text)["data"]

    def _log_debug(self, message):
        if self.debug_mode:
            self.add_message(f"[DEBUG] {message}", 0)

    def _normalize_problem_id(self, problem_id):
        if problem_id is None:
            return None
        return str(problem_id)

    def _resolve_problem_id(self, source, fallback=None):
        if isinstance(source, dict):
            for key in ("problemId", "sid", "problemid", "id"):
                if key in source and source[key] is not None:
                    return self._normalize_problem_id(source[key])
        if fallback is not None:
            return self._normalize_problem_id(fallback)
        return None

    def _format_limit_text(self, limit):
        if limit is None:
            return "请尽快查看"
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            return "请尽快查看"
        if limit_int == -1:
            return "不限时"
        if limit_int < 0:
            return "即将截止"
        return f"剩余约{limit_int}秒"

    def _notify_problem_release(self, problem_id, limit):
        normalized_id = self._normalize_problem_id(problem_id)
        if normalized_id is not None and normalized_id in self.notified_problems:
            return

        page_no = None
        if normalized_id is not None:
            page_no = self.problem_page_map.get(normalized_id)
        if page_no is None:
            self._log_debug(f"题目 {normalized_id} 未找到页码映射")
        page_text = f"第{page_no}页" if page_no is not None else "未知页"
        limit_text = self._format_limit_text(limit)
        self.add_message(f"{self.lessonname} {page_text}发布新题（{limit_text}）", 3)

        if normalized_id is not None:
            self.notified_problems.add(normalized_id)

        if self.config.get("auto_answer") and not self.auto_answer_warned:
            self.add_message(f"{self.lessonname} 当前版本不支持自动答题，请手动作答。", 4)
            self.auto_answer_warned = True

    def _extract_page_number(self, data):
        if not isinstance(data, dict):
            return None

        def normalize_index(key, value):
            if isinstance(value, (int, float)):
                value = int(value)
                if key.lower().endswith("index") or key.lower().endswith("idx") or key == "index":
                    return value + 1
                return value
            return None

        for key in ("page", "pageNo", "pageIndex", "page_index", "currentPage", "index"):
            if key in data:
                normalized = normalize_index(key, data.get(key))
                if normalized is not None:
                    return normalized

        for key in ("slide", "currentSlide", "msg", "payload"):
            nested = data.get(key)
            if isinstance(nested, dict):
                result = self._extract_page_number(nested)
                if result is not None:
                    return result

        return None

    def _handle_presentation_change(self, data):
        if not isinstance(data, dict):
            return
        presentation_id = data.get("presentation")
        page_no = self._extract_page_number(data)
        if page_no is None:
            self._log_debug(f"presentation {presentation_id} 未提取到页码: {list(data.keys())}")
            return
        if presentation_id is not None:
            last_page = self.current_presentation_page.get(presentation_id)
            if last_page == page_no:
                return
            self.current_presentation_page[presentation_id] = page_no
        self.add_message(f"{self.lessonname} 当前 PPT 第{page_no}页", 0)

    def get_problems(self, presentationid):
        # 获取课程ppt中的题目，只汇总题目所在页码
        try:
            data = self._get_ppt(presentationid)
            slides = data.get("slides")
            if not isinstance(slides, list):
                self.add_message(f"{self.lessonname} 读取 PPT {presentationid} 数据失败：缺少有效的 slides", 4)
                self._log_debug(f"PPT {presentationid} slides 类型: {type(slides).__name__}")
                return []

            total_slides = len(slides)
            self.add_message(f"{self.lessonname} PPT {presentationid} 共 {total_slides} 页", 0)

            problem_pages = self.ppt_problem_pages.setdefault(presentationid, set())
            pages_before = set(problem_pages)
            added_pages = set()

            for index, slide in enumerate(slides):
                problem = slide.get("problem")
                if not isinstance(problem, dict):
                    if problem is not None:
                        self._log_debug(f"PPT {presentationid} 第{index + 1}页 problem 类型: {type(problem).__name__}")
                    continue

                problem_id = self._normalize_problem_id(problem.get("problemId"))
                if problem_id is None:
                    self._log_debug(f"PPT {presentationid} 第{index + 1}页 problem 缺少 problemId")
                    continue

                page_no = index + 1
                if page_no not in problem_pages:
                    added_pages.add(page_no)
                problem_pages.add(page_no)
                self.problem_page_map[problem_id] = page_no

                content_type = type(problem.get("content")).__name__
                if content_type not in self._seen_content_types:
                    self._seen_content_types.add(content_type)
                    self._log_debug(f"题目 {problem_id} content 类型: {content_type}")

                answers_raw = problem.get("answers")
                answers_type = type(answers_raw).__name__
                if answers_raw is not None and answers_type not in self._seen_answers_types:
                    self._seen_answers_types.add(answers_type)
                    self._log_debug(f"题目 {problem_id} answers 类型: {answers_type}")

                self.problem_cache[problem_id] = problem

            if problem_pages:
                pages_text = ", ".join(str(page) for page in sorted(problem_pages))
                if not pages_before:
                    self.add_message(f"{self.lessonname} PPT {presentationid} 题目页数：{pages_text}", 0)
                elif added_pages:
                    added_text = ", ".join(str(page) for page in sorted(added_pages))
                    self.add_message(f"{self.lessonname} PPT {presentationid} 题目页数更新：{pages_text}（新增 {added_text}）", 0)
                else:
                    self._log_debug(f"PPT {presentationid} 题目页数无变化")
            else:
                self.add_message(f"{self.lessonname} PPT {presentationid} 暂未发现题目", 0)

            return sorted(problem_pages)

        except Exception as e:
            self.add_message(f"{self.lessonname} 获取 PPT {presentationid} 题目时发生错误: {e}", 4)
            self._log_debug(f"get_problems 异常: {e}")
            return []

    def on_open(self, wsapp):
        self.handshark = {"op":"hello","userid":self.user_uid,"role":"student","auth":self.auth,"lessonid":self.lessonid}
        wsapp.send(json.dumps(self.handshark))

    def checkin_class(self):
        r = requests.post(url="https://changjiang.yuketang.cn/api/v3/lesson/checkin",headers=self.headers,data=json.dumps({"source":5,"lessonId":self.lessonid}),proxies={"http": None,"https":None})
        set_auth = r.headers.get("Set-Auth",None)
        times = 1
        while not set_auth and times <= 3:
            set_auth = r.headers.get("Set-Auth",None)
            times += 1
            time.sleep(1)
        self.headers["Authorization"] = "Bearer %s" % set_auth
        return dict_result(r.text)["data"]["lessonToken"]

    def on_message(self, wsapp, message):
        data = dict_result(message)
        op = data.get("op")
        if op == "hello":
            timeline = data.get("timeline", [])
            presentations = {slide.get("pres") for slide in timeline if isinstance(slide, dict) and slide.get("type") == "slide" and slide.get("pres")}
            current_presentation = data.get("presentation")
            if current_presentation:
                presentations.add(current_presentation)
            for presentationid in presentations:
                self.get_problems(presentationid)
            self._handle_presentation_change(data)
            self.unlocked_problem = data.get("unlockedproblem", [])
            for problemid in self.unlocked_problem:
                self._current_problem(wsapp, problemid)
        elif op == "unlockproblem":
            problem = data.get("problem", {})
            problem_id = self._resolve_problem_id(problem)
            limit = problem.get("limit")
            self._notify_problem_release(problem_id, limit)
        elif op == "lessonfinished":
            meg = "%s下课了" % self.lessonname
            self.add_message(meg,7)
            wsapp.close()
        elif op == "presentationupdated":
            self.get_problems(data.get("presentation"))
            self._handle_presentation_change(data)
        elif op == "presentationcreated":
            self.get_problems(data.get("presentation"))
            self._handle_presentation_change(data)
        elif op == "newdanmu" and self.config["auto_danmu"]:
            current_content = data["danmu"].lower()
            uid = data["userid"]
            sent_danmu_user = User(uid)
            if sent_danmu_user in self.classmates_ls:
                for i in self.classmates_ls:
                    if i == sent_danmu_user:
                        meg = "%s课程的%s%s发送了弹幕：%s" %(self.lessonname,i.sno,i.name,data["danmu"])
                        self.add_message(meg,2)
                        break
            else:
                self.classmates_ls.append(sent_danmu_user)
                sent_danmu_user.get_userinfo(self.classroomid,self.headers)
                meg = "%s课程的%s%s发送了弹幕：%s" %(self.lessonname,sent_danmu_user.sno,sent_danmu_user.name,data["danmu"])
                self.add_message(meg,2)
            now = time.time()
            # 收到一条弹幕，尝试取出其之前的所有记录的列表，取不到则初始化该内容列表
            try:
                same_content_ls = self.danmu_dict[current_content]
            except KeyError:
                self.danmu_dict[current_content] = []
                same_content_ls = self.danmu_dict[current_content]
            # 清除超过60秒的弹幕记录
            for i in same_content_ls:
                if now - i > 60:
                    same_content_ls.remove(i)
            # 如果当前的弹幕没被发过，或者已发送时间超过60秒
            if current_content not in self.sent_danmu_dict.keys() or now - self.sent_danmu_dict[current_content] > 60:
                if len(same_content_ls) + 1 >= self.config["danmu_config"]["danmu_limit"]:
                    self.send_danmu(current_content)
                    same_content_ls = []
                    self.sent_danmu_dict[current_content] = now
                else:
                    same_content_ls.append(now)
        elif op == "callpaused":
            meg = "%s点名了，点到了：%s" % (self.lessonname, data["name"])
            if self.user_uname == data["name"]:
                self.add_message(meg,5)
            else:
                self.add_message(meg,6)
        # 程序在上课中途运行，由_current_problem发送的已解锁题目数据，得到的返回值。
        # 此处需要筛选未到期的题目进行回答。
        elif op == "probleminfo":
            raw_limit = data.get("limit")
            time_left = None
            try:
                limit_int = int(raw_limit)
            except (TypeError, ValueError):
                limit_int = None
            if limit_int == -1:
                time_left = -1
            elif limit_int is not None:
                try:
                    delta = int(data.get("now", 0)) - int(data.get("dt", 0))
                    time_left = int(limit_int - delta / 1000)
                except Exception:
                    time_left = limit_int
            problem_id = self._resolve_problem_id(data)
            self._notify_problem_release(problem_id, time_left)

    def _current_problem(self, wsapp, promblemid):
        # 为获取已解锁的问题详情信息，向wsapp发送probleminfo
        query_problem = {"op":"probleminfo","lessonid":self.lessonid,"problemid":promblemid,"msgid":1}
        wsapp.send(json.dumps(query_problem))
    
    def start_lesson(self, callback):
        self.auth = self.checkin_class()
        rtn = self.get_lesson_info()
        teacher = rtn["teacher"]["name"]
        title = rtn["title"]
        timestamp = rtn["startTime"] // 1000
        time_str = time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(timestamp))
        index = self.main_ui.tableWidget.rowCount()
        self.add_course([self.lessonname,title,teacher,time_str],index)
        self.wsapp = websocket.WebSocketApp(url=wss_url,header=self.headers,on_open=self.on_open,on_message=self.on_message)
        self.wsapp.run_forever()
        meg = "%s监听结束" % self.lessonname
        self.add_message(meg,7)
        self.del_course(index)
        # threading.Thread(target=say_something,args=(meg,)).start()
        return callback(self)
    
    def send_danmu(self,content):
        url = "https://changjiang.yuketang.cn/api/v3/lesson/danmu/send"
        data = {
            "extra": "",
            "fromStart": "50",
            "lessonId": self.lessonid,
            "message": content,
            "requiredCensor": False,
            "showStatus": True,
            "target": "",
            "userName": "",
            "wordCloud": True
        }
        r = requests.post(url=url,headers=self.headers,data=json.dumps(data),proxies={"http": None,"https":None})
        if dict_result(r.text)["code"] == 0:
            meg = "%s弹幕发送成功！内容：%s" % (self.lessonname,content)
        else:
            meg = "%s弹幕发送失败！内容：%s" % (self.lessonname,content)
        self.add_message(meg,1)
    
    def get_lesson_info(self):
        url = "https://changjiang.yuketang.cn/api/v3/lesson/basic-info"
        r = requests.get(url=url,headers=self.headers,proxies={"http": None,"https":None})
        return dict_result(r.text)["data"]
        

    def __eq__(self, other):
        return self.lessonid == other.lessonid

class User:
    def __init__(self, uid):
        self.uid = uid
    
    def get_userinfo(self, classroomid, headers):
        r = requests.get("https://changjiang.yuketang.cn/v/course_meta/fetch_user_info_new?query_user_id=%s&classroom_id=%s" % (self.uid,classroomid),headers=headers,proxies={"http": None,"https":None})
        data = dict_result(r.text)["data"]
        self.sno = data["school_number"]
        self.name = data["name"]