# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-节点管理(BlueKing-BK-NODEMAN) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.utils.translation import ugettext_lazy as _

from apps.backend.api import constants as const
from apps.backend.exceptions import GenCommandsError
from apps.node_man import constants, models
from apps.node_man.models import aes_cipher
from apps.utils.basic import suffix_slash


class InstallationTools:
    def __init__(
        self,
        script_file_name: str,
        dest_dir: str,
        win_commands: List[str],
        upstream_nodes: List[str],
        jump_server: models.Host,
        pre_commands: List[str],
        run_cmd: str,
        host: models.Host,
        ap: models.AccessPoint,
        identity_data: models.IdentityData,
        proxies: List[models.Host],
    ):
        """
        :param script_file_name: 脚本名称，如 setup_agent.sh
        :param dest_dir: 目标目录，通常为 /tmp 通过接入点配置获取
        :param win_commands: Windows执行命令
        :param upstream_nodes: 上游节点，通常为proxy或者安装通道指定的商用
        :param jump_server: 跳板服务器，通常为proxy或者安装通道的跳板机
        :param pre_commands: 预执行命令，目前仅 Windows 需要，提前推送 curl.exe 等工具
        :param run_cmd: 运行命令，通过 format_run_cmd_by_os_type 方法生成
        :param host: 主机对象
        :param ap: 接入点对象
        :param identity_data: 认证数据对象
        :param proxies: 代理列表
        """
        self.script_file_name = script_file_name
        self.dest_dir = dest_dir
        self.win_commands = win_commands
        self.upstream_nodes = upstream_nodes
        self.jump_server = jump_server
        self.pre_commands = pre_commands
        self.run_cmd = run_cmd
        self.host = host
        self.ap = ap
        self.identity_data = identity_data
        self.proxies = proxies


def gen_nginx_download_url(nginx_ip: str) -> str:
    return f"http://{nginx_ip}:{settings.BK_NODEMAN_NGINX_DOWNLOAD_PORT}/"


def fetch_gse_servers(
    host: models.Host,
    host_ap: models.AccessPoint,
    proxies: List[models.Host],
    install_channel: Tuple[models.Host, Dict[str, List]],
) -> Tuple:
    jump_server = None
    if host.install_channel_id:
        # 指定安装通道时，由安装通道生成相关配置
        jump_server, upstream_servers = install_channel
        bt_file_servers = ",".join(upstream_servers["btfileserver"])
        data_servers = ",".join(upstream_servers["dataserver"])
        task_servers = ",".join(upstream_servers["taskserver"])
        package_url = gen_nginx_download_url(jump_server.inner_ip)
        default_callback_url = (
            settings.BKAPP_NODEMAN_CALLBACK_URL
            if host.node_type == constants.NodeType.AGENT
            else settings.BKAPP_NODEMAN_OUTER_CALLBACK_URL
        )
        callback_url = host_ap.outer_callback_url or default_callback_url
        return jump_server, bt_file_servers, data_servers, data_servers, task_servers, package_url, callback_url

    if host.node_type == constants.NodeType.AGENT:
        bt_file_servers = ",".join(server["inner_ip"] for server in host_ap.btfileserver)
        data_servers = ",".join(server["inner_ip"] for server in host_ap.dataserver)
        task_servers = ",".join(server["inner_ip"] for server in host_ap.taskserver)
        package_url = host_ap.package_inner_url
        callback_url = settings.BKAPP_NODEMAN_CALLBACK_URL
    elif host.node_type == constants.NodeType.PROXY:
        bt_file_servers = ",".join(server["outer_ip"] for server in host_ap.btfileserver)
        data_servers = ",".join(server["outer_ip"] for server in host_ap.dataserver)
        task_servers = ",".join(server["outer_ip"] for server in host_ap.taskserver)
        package_url = host_ap.package_outer_url
        # 不同接入点使用不同的callback_url默认情况下接入点callback_url为空，先取接入点，为空的情况下使用原来的配置
        callback_url = host_ap.outer_callback_url or settings.BKAPP_NODEMAN_OUTER_CALLBACK_URL
    else:
        # PAGENT的场景
        proxy_ips = list(set([proxy.inner_ip for proxy in proxies]))
        jump_server = host.get_random_alive_proxy(proxies)
        bt_file_servers = ",".join(ip for ip in proxy_ips)
        data_servers = ",".join(ip for ip in proxy_ips)
        task_servers = ",".join(ip for ip in proxy_ips)
        package_url = host_ap.package_outer_url
        # 不同接入点使用不同的callback_url默认情况下接入点callback_url为空，先取接入点，为空的情况下使用原来的配置
        callback_url = host_ap.outer_callback_url or settings.BKAPP_NODEMAN_OUTER_CALLBACK_URL

    return jump_server, bt_file_servers, data_servers, data_servers, task_servers, package_url, callback_url


def choose_script_file(host: models.Host) -> str:
    """选择脚本文件"""
    if host.node_type == constants.NodeType.PROXY:
        # proxy 安装
        return "setup_proxy.sh"

    if host.install_channel_id or host.node_type == constants.NodeType.PAGENT:
        # 远程安装 P-AGENT 或者指定安装通道时，用 setup_pagent 脚本
        return constants.SetupScriptFileName.SETUP_PAGENT_PY.value

    # 其它场景，按操作系统来区分
    script_file_name = constants.SCRIPT_FILE_NAME_MAP[host.os_type]
    return script_file_name


def format_run_cmd_by_os_type(os_type: str, run_cmd=None) -> str:
    os_type = os_type.lower()
    if os_type == const.OS.WINDOWS and run_cmd:
        return run_cmd
    suffix = const.SUFFIX_MAP[os_type]
    if suffix != const.SUFFIX_MAP[const.OS.AIX]:
        shell = "bash"
    else:
        shell = suffix
    run_cmd = f"nohup {shell} {run_cmd} &" if run_cmd else shell
    return run_cmd


def gen_commands(
    host: models.Host,
    pipeline_id: str,
    is_uninstall: bool,
    identity_data: Optional[models.IdentityData] = None,
    host_ap: Optional[models.AccessPoint] = None,
    proxies: Optional[List[models.Host]] = None,
    install_channel: Optional[Tuple[models.Host, Dict[str, List]]] = None,
) -> InstallationTools:
    """
    生成安装命令
    :param host: 主机信息
    :param pipeline_id: Node ID
    :param is_uninstall: 是否卸载
    :param identity_data: 主机认证数据对象
    :param host_ap: 主机接入点对象
    :param proxies: 主机代理列表
    :param install_channel: 安装通道
    :return: dest_dir 目标目录, win_commands: Windows安装命令, proxies 代理列表,
             proxy 云区域所使用的代理, pre_commands 安装前命令, run_cmd 安装命令
    """
    # 批量场景请传入Optional所需对象，以避免 n+1 查询，提高执行效率
    host_ap = host_ap or host.ap
    identity_data = identity_data or host.identity
    install_channel = install_channel or host.install_channel
    proxies = proxies or host.proxies
    win_commands = []
    (
        jump_server,
        bt_file_servers,
        data_servers,
        data_servers,
        task_servers,
        package_url,
        callback_url,
    ) = fetch_gse_servers(host, host_ap, proxies, install_channel)
    upstream_nodes = task_servers
    agent_config = host_ap.get_agent_config(host.os_type)
    # 安装操作
    install_path = agent_config["setup_path"]
    token = aes_cipher.encrypt(f"{host.inner_ip}|{host.bk_cloud_id}|{pipeline_id}|{time.time()}")
    port_config = host_ap.port_config
    run_cmd_params = [
        f"-s {pipeline_id}",
        f"-r {callback_url}",
        f"-l {package_url}",
        f"-c {token}",
        f'-O {port_config.get("io_port")}',
        f'-E {port_config.get("file_svr_port")}',
        f'-A {port_config.get("data_port")}',
        f'-V {port_config.get("btsvr_thrift_port")}',
        f'-B {port_config.get("bt_port")}',
        f'-S {port_config.get("bt_port_start")}',
        f'-Z {port_config.get("bt_port_end")}',
        f'-K {port_config.get("tracker_port")}',
        f'-e "{bt_file_servers}"',
        f'-a "{data_servers}"',
        f'-k "{task_servers}"',
    ]

    check_run_commands(run_cmd_params)
    script_file_name = choose_script_file(host)

    dest_dir = agent_config["temp_path"]
    dest_dir = suffix_slash(host.os_type.lower(), dest_dir)
    if script_file_name == constants.SetupScriptFileName.SETUP_PAGENT_PY.value:
        run_cmd_params.append(f"-L {settings.DOWNLOAD_PATH}")
        # P-Agent在proxy上执行，proxy都是Linux机器
        dest_dir = host_ap.get_agent_config(constants.OsType.LINUX)["temp_path"]
        dest_dir = suffix_slash(constants.OsType.LINUX, dest_dir)
        if host.is_manual:
            run_cmd_params.insert(0, f"{dest_dir}{script_file_name} ")
        host_tmp_path = suffix_slash(host.os_type.lower(), agent_config["temp_path"])
        host_identity = (
            identity_data.key if identity_data.auth_type == constants.AuthType.KEY else identity_data.password
        )
        host_shell = format_run_cmd_by_os_type(host.os_type)
        run_cmd_params.extend(
            [
                f"-HLIP {host.login_ip or host.inner_ip}",
                f"-HIIP {host.inner_ip}",
                f"-HA {identity_data.account}",
                f"-HP {identity_data.port}",
                f"-HI '{host_identity}'",
                f"-HC {host.bk_cloud_id}",
                f"-HNT {host.node_type}",
                f"-HOT {host.os_type.lower()}",
                f"-HDD '{host_tmp_path}'",
                f"-HPP '{settings.BK_NODEMAN_NGINX_PROXY_PASS_PORT}'",
                f"-HSN '{constants.SCRIPT_FILE_NAME_MAP[host.os_type]}'",
                f"-HS '{host_shell}'",
            ]
        )

        run_cmd_params.extend(
            [
                f"-p '{install_path}'",
                f"-I {jump_server.inner_ip}",
                f"-o {gen_nginx_download_url(jump_server.inner_ip)}",
                "-R" if is_uninstall else "",
            ]
        )

        # 通道特殊配置
        if host.install_channel_id:
            __, upstream_servers = host.install_channel
            agent_download_proxy = upstream_servers.get("agent_download_proxy", True)
            if agent_download_proxy:
                # 打开agent下载代理选项时传入
                run_cmd_params.extend([f"-ADP '{agent_download_proxy}'"])
            channel_proxy_address = upstream_servers.get("channel_proxy_address", None)
            if channel_proxy_address:
                run_cmd_params.extend([f"-CPA '{channel_proxy_address}'"])

        run_cmd = " ".join(run_cmd_params)

        download_cmd = (
            f"if [ ! -e {dest_dir}{script_file_name} ] || "
            f"[ `curl {package_url}/{script_file_name} -s | md5sum | awk '{{print $1}}'` "
            f"!= `md5sum {dest_dir}{script_file_name} | awk '{{print $1}}'` ]; then "
            f"curl {package_url}/{script_file_name} -o {dest_dir}{script_file_name} --connect-timeout 5 -sSf "
            f"&& chmod +x {dest_dir}{script_file_name}; fi"
        )
    else:
        run_cmd_params.extend(
            [
                f"-i {host.bk_cloud_id}",
                f"-I {host.inner_ip}",
                "-N SERVER",
                f"-p {install_path}",
                f"-T {dest_dir}",
                "-R" if is_uninstall else "",
            ]
        )

        run_cmd = format_run_cmd_by_os_type(host.os_type, f"{dest_dir}{script_file_name} {' '.join(run_cmd_params)}")
        if host.os_type == constants.OsType.WINDOWS:
            # WINDOWS 下的 Agent 安装
            win_remove_cmd = (
                f"del /q /s /f {dest_dir}{script_file_name} "
                f"{dest_dir}{constants.SetupScriptFileName.GSECTL_BAT.value}"
            )
            win_download_cmd = (
                f"{dest_dir}curl.exe {host_ap.package_inner_url}/{script_file_name}"
                f" -o {dest_dir}{script_file_name} -sSf"
            )

            win_commands = [win_remove_cmd, win_download_cmd, run_cmd]
        download_cmd = f"curl {package_url}/{script_file_name} -o {dest_dir}{script_file_name} --connect-timeout 5 -sSf"
    chmod_cmd = f"chmod +x {dest_dir}{script_file_name}"
    pre_commands = [
        download_cmd,
        chmod_cmd,
    ]
    if Path(dest_dir) != Path("/tmp"):
        pre_commands.insert(0, f"mkdir -p {dest_dir}")

    upstream_nodes = list(set(upstream_nodes))
    return InstallationTools(
        script_file_name,
        dest_dir,
        win_commands,
        upstream_nodes,
        jump_server,
        pre_commands,
        run_cmd,
        host,
        host_ap,
        identity_data,
        proxies,
    )


def check_run_commands(run_commands):
    for command in run_commands:
        if command.startswith("-r"):
            if not re.match("^-r https?://.+/backend$", command):
                raise GenCommandsError(context=_("CALLBACK_URL不符合规范, 请联系运维人员修改。 例：http://domain.com/backend"))


def batch_gen_commands(hosts: List[models.Host], pipeline_id: str, is_uninstall: bool) -> Dict[int, InstallationTools]:
    """批量生成安装命令"""
    # 批量查出主机的属性并设置为property，避免在循环中进行ORM查询，提高效率
    host_id__installation_tool_map = {}
    ap_id_obj_map = models.AccessPoint.ap_id_obj_map()
    bk_host_ids = [host.bk_host_id for host in hosts]
    host_id_identity_map = {
        identity.bk_host_id: identity for identity in models.IdentityData.objects.filter(bk_host_id__in=bk_host_ids)
    }
    install_channel_id_obj_map = {}
    cloud_id_proxies_map = {}

    for host in hosts:
        host_ap = ap_id_obj_map[host.ap_id]
        # 避免部分主机认证信息丢失的情况下，通过host.identity重新创建来兜底保证不会异常
        identity_data = host_id_identity_map.get(host.bk_host_id) or host.identity
        # 缓存相同云区域的proxies，提高性能，大部分场景下同时安装的P-Agent都属于同一个云区域
        if host.bk_cloud_id not in cloud_id_proxies_map:
            cloud_id_proxies_map[host.bk_cloud_id] = host.proxies
        proxies = cloud_id_proxies_map[host.bk_cloud_id]

        # 同理缓存安装通道
        if host.install_channel_id not in install_channel_id_obj_map:
            install_channel_id_obj_map[host.install_channel_id] = host.install_channel
        install_channel = install_channel_id_obj_map[host.install_channel_id]

        host_id__installation_tool_map[host.bk_host_id] = gen_commands(
            host, pipeline_id, is_uninstall, identity_data, host_ap, proxies, install_channel
        )

    return host_id__installation_tool_map
