import re
import time
import requests
from datetime import datetime, timedelta
import pytz
from typing import Any, List, Dict, Tuple, Optional
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings
from app.helper.cookiecloud import CookieCloudHelper
from playwright.sync_api import sync_playwright

class WeWorkIPPW(_PluginBase):
    # 插件名称
    plugin_name = "企微自动配置IPpw版"
    # 插件描述
    plugin_desc = "!!docker用户用这个版本!!定时获取最新动态公网IP，配置到企业微信应用的可信IP列表里。"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "suraxiuxiu"
    # 作者主页
    author_url = "https://github.com/suraxiuxiu"
    # 插件配置项ID前缀
    plugin_config_prefix = "weworkippw_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 2

    #匹配ip地址的正则
    _ip_pattern = r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'
    #获取ip地址的网址列表
    _ip_urls = ["https://myip.ipip.net", "https://ddns.oray.com/checkip", "https://ip.3322.net","https://4.ipw.cn"]
    #当前ip地址
    _current_ip_address = '192.168.1.1'
    #企业微信应用管理地址
    _wechatUrl=f'https://work.weixin.qq.com/wework_admin/frame#/apps/modApiApp/00000000000'
    #登录cookie
    _cookie_header = ""
    #覆盖已填写的IP,设置FALSE则添加新IP到已有IP列表里
    _overwrite = True

    #使用CookieCloud开关
    _use_cookiecloud = True
    #cookie有效检测
    _cookie_valid = False
    #IP更改成功状态,防止检测IP改动但cookie失效的时候_current_ip_address已经更新成新IP导致后面刷新cookie也没有更改企微IP
    _ip_changed = False
    #检测间隔时间,默认10分钟,太久会导致cookie失效
    _refresh_cron = '*/10 * * * *'
    _cron = None
    _enabled = False
    _onlyonce = False
    _cookiecloud = CookieCloudHelper()

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._wechatUrl = ''
        self._cookie_header = ""
        self._overwrite = True
        self._use_cookiecloud = True
        self._cookie_valid = False
        self._ip_changed = False

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._wechatUrl = config.get("wechatUrl")
            self._cookie_header = config.get("cookie_header")
            self._overwrite = config.get("overwrite")
            self._current_ip_address = config.get("current_ip_address")
            self._use_cookiecloud = config.get("use_cookiecloud")
            self._ip_changed = config.get("ip_changed")
            
        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)       
            # 运行一次定时服务
            if self._onlyonce:
                logger.info("立即检测公网IP")
                self._scheduler.add_job(func=self.check, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="检测公网IP")
                # 关闭一次性开关
                self._onlyonce = False

            # 固定半小时周期请求一次地址,防止cookie失效        
            try:
                self._scheduler.add_job(func=self.refresh_cookie,
                                        trigger=CronTrigger.from_crontab(self._refresh_cron),
                                        name="延续企业微信cookie有效时间")
            except Exception as err:
                logger.error(f"定时任务配置错误：{err}")
                self.systemmessage.put(f"执行周期配置错误：{err}")
                
            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()
        self.__update_config()        
            
    @eventmanager.register(EventType.PluginAction)
    def check(self, event: Event = None):
        """
        检测函数
        """
        if not self._enabled:
            logger.error("插件未开启")
            return

        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "weworkippw":
                return
            logger.info("收到命令，开始检测公网IP ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始检测公网IP ...",
                              userid=event.event_data.get("user"))

        logger.info("开始检测公网IP")
        if self.CheckIP():
            self.ChangeIP()
            self.__update_config()

        logger.info("检测公网IP完毕")
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="检测公网IP完毕",
                              userid=event.event_data.get("user"))
        
    def CheckIP(self):
        if not self._cookie_valid:
            self.refresh_cookie()
            if not self._cookie_valid:
                logger.error("请求企微失败,cookie可能过期,跳过IP检测")
                return False
        if not self._ip_changed:#上次IP变更没有改动到企微 再次请求该IP
            return True
        for url in self._ip_urls:
            ip_address = self.get_ip_from_url(url)
            if ip_address != "获取IP失败":
                logger.info(f"IP获取成功: {url}: {ip_address}")
                break
            else:
                logger.error(f"请求网址失败: {url}")
        if ip_address == "获取IP失败":
            logger.error("获取IP失败") 
            return False      
        if ip_address != self._current_ip_address:
            logger.info("检测到IP变化")
            self._current_ip_address = ip_address
            self._ip_changed = False
            return True
        else:
            #logger.info("公网IP未变化")
            return False
            
    def get_ip_from_url(self,url):
        try:
            # 发送 GET 请求
            response = requests.get(url)
        
            # 检查响应状态码是否为 200
            if response.status_code == 200:
                # 解析响应 JSON 数据并获取 IP 地址
                ip_address = re.search(self._ip_pattern, response.text)
                if ip_address:
                    return ip_address.group()
                else:
                    return "获取IP失败"
            else:
                return "获取IP失败"
        except Exception as e:
            logger.warning(f"{url}获取IP失败,Error: {e}")
            return "获取IP失败"
            
    def ChangeIP(self):
        logger.info("开始请求企业微信管理更改可信IP")
        # 解析 Cookie 字符串为字典
        try:    
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                cookie = self.get_cookie()
                context.add_cookies(cookie)
                page = context.new_page()
                page.goto(self._wechatUrl)
                time.sleep(1)
                login = page.locator('.login_stage_title_text')
                # 检查登录元素是否可见
                if login.is_visible():
                    logger.info("cookie失效,请重新获取")
                    self._cookie_valid = False
                    return
                else:
                    logger.info("加载企微管理界面成功")
                    self._cookie_valid = True
                logger.info("正在更改IP地址")
                open_ip_config = page.locator('div.app_card_operate.js_show_ipConfig_dialog')
                open_ip_config.click()
                page.wait_for_selector('textarea.js_ipConfig_textarea')
                input_area = page.locator('textarea.js_ipConfig_textarea')
                confirm = page.locator('.js_ipConfig_confirmBtn')
                existing_ip = input_area.input_value()
                if self._overwrite:
                    input_area.fill(self._current_ip_address)
                else:
                    input_area.fill(f'{existing_ip};{self.current_ip_address}')
                confirm.click()
                time.sleep(1)
                logger.info("更改IP地址成功")
                self._ip_changed = True
                browser.close() 
        except Exception as e:
                logger.error(f"更改可信IP失败:{e}") 
                self._cookie_valid = False    
    
    def refresh_cookie(self):
        try:    
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context()
                cookie = self.get_cookie()
                context.add_cookies(cookie)
                page = context.new_page()
                page.goto(self._wechatUrl)
                login = page.locator('.login_stage_title_text')
            # 检查登录元素是否可见
                if login.is_visible():
                    logger.info("cookie失效,请重新获取")
                    self._cookie_valid = False
                else:
                    logger.info("cookie有效校验成功")
                    self._cookie_valid = True
                browser.close()
        except Exception as e:
                logger.error(f"cookie校验失败:{e}") 
                self._cookie_valid = False   
    
    def parse_cookie_header(self,cookie_header):
        cookies = []
        for cookie in cookie_header.split(';'):
            name, value = cookie.strip().split('=', 1)
            cookies.append({
                'name': name,
                'value': value,
                'domain': '.work.weixin.qq.com',
                'path': '/'
            })
        return cookies
    
    def get_cookie(self):
        try:
            cookie_header = ''
            if self._use_cookiecloud:
                logger.info("尝试从CookieCloud同步企微cookie ...")
                cookies, msg = self._cookiecloud.download()
                if not cookies:
                    logger.error(f"CookieCloud获取cookie失败,将使用手动配置cookie,失败原因：{msg}")
                    cookie_header = self._cookie_header
                else:
                    for domain, cookie in cookies.items():
                        if domain == ".work.weixin.qq.com":
                            cookie_header = cookie
                            break
                    if cookie_header == '':
                        cookie_header = self._cookie_header
            else:                
                cookie_header = self._cookie_header
            cookie = self.parse_cookie_header(cookie_header)
            return cookie
        except Exception as e:
                logger.error(f"获取cookie失败:{e}") 
                return cookie
    
    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "wechatUrl": self._wechatUrl,
            "cookie_header": self._cookie_header,
            "overwrite": self._overwrite,
            "current_ip_address": self._current_ip_address,
            "use_cookiecloud": self._use_cookiecloud,
            "ip_changed": self._ip_changed
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/weworkippw",
            "event": EventType.PluginAction,
            "desc": "微信应用检测动态IP",
            "category": "",
            "data": {
                "action": "weworkippw"
            }
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "WeWorkIPPW",
                "name": "微信应用自动配置动态公网IP",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.check,
                "kwargs": {}
            }]
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即检测一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'overwrite',
                                            'label': '覆盖模式',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'use_cookiecloud',
                                            'label': '使用CookieCloud获取cookie',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '检测周期',
                                            'placeholder': '0 * * * *'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cookie_header',
                                            'label': 'COOKIE',
                                            'rows': 1,
                                            'placeholder': '登录企微后导出HeaderString格式的cookie填到此处,默认使用CookieCloud获取Cookie,如果获取失败会尝试使用此处填写的Cookie'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'wechatUrl',
                                            'label': '应用网址',
                                            'rows': 1,
                                            'placeholder': '企业微信应用的管理网址,类似于https://work.weixin.qq.com/wework_admin/frame#/apps/modApiApp/00000000000'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '覆盖模式: 开启后新IP会直接覆写到已填写的IP列表,关闭则把新IP添加到已有列表里'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '检测周期：获取动态公网IP的间隔,推荐几分钟检测一次,有新IP才会请求企业微信管理更改'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'cookie需填入HeaderString的格式,后台固定间隔验证一次cookie,和这里设置的检测周期无关。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'cookie获取插件：https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '微信通知代理地址记得改回https://qyapi.weixin.qq.com/并重启MP'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "cron": "",
            "overwrite": False,
            "use_cookiecloud": True,
            "onlyonce": False,
            "cookie_header": "",
            "wechatUrl": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))