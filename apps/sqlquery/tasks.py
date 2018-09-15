# -*- coding:utf-8 -*-
# edit by fuzongfei
import datetime
import logging
import re
import subprocess

import pymysql
from celery import shared_task

from sqlorders.models import MysqlConfig, MysqlSchemas

logger = logging.getLogger(__name__)


@shared_task
def sync_schemas():
    ignored_params = ('information_schema', 'mysql', 'percona', 'performance_schema', 'sys')
    schema_filter_query = f"select schema_name from information_schema.schemata " \
                          f"where SCHEMA_NAME not in {ignored_params}"

    collect_from_host = []
    for row in MysqlConfig.objects.all():
        collect_from_host.append({
            'user': row.user,
            'password': row.password,
            'db_host': row.host,
            'db_port': row.port,
            'envi_id': row.envi_id,
            'is_master': row.is_master,
            'comment': row.comment
        })

    # 连接到目标数据库，统计schema
    for row in collect_from_host:
        try:
            cnx = pymysql.connect(user=row['user'],
                                  password=row['password'],
                                  host=row['db_host'],
                                  port=row['db_port'],
                                  charset='utf8mb4',
                                  cursorclass=pymysql.cursors.DictCursor)
            try:
                with cnx.cursor() as cursor:
                    cursor.execute(schema_filter_query)
                    for i in cursor.fetchall():
                        MysqlSchemas.objects.update_or_create(
                            user=row['user'],
                            password=row['password'],
                            host=row['db_host'],
                            port=row['db_port'],
                            schema=i['schema_name'],
                            envi_id=row['envi_id'],
                            is_master=row['is_master'],
                            schema_join='_'.join(
                                ([row['db_host'], str(row['db_port']), i['schema_name']])),
                            comment=row['comment'],
                        )
            finally:
                cnx.close()
        except Exception as err:
            logger.error(err)
            continue


def check_rules(abstract, rule):
    if abstract == '':
        return False
    else:
        if not re.search(rule, abstract, re.I):
            return True
        else:
            return False


@shared_task
def detect_deadlock():
    # webhook
    if Webhook.objects.filter().first():
        webhook_addr = Webhook.objects.get().webhook_addr
        xiaoding = DingtalkChatbot(webhook_addr)
        # 检查实例，并生生成实例死锁记录的命令
        # 使用本机的数据库作为死锁记录
        # 库名：auditsql，表名：dbaudit_deadlocks_records
        command = "/usr/bin/pt-deadlock-logger --user={user} --password={password} --host={host} --port={port} " \
                  "--no-version-check --create-dest-table " \
                  "--dest h=localhost,u=root,p=123.com,D=auditsql,t=dbaudit_deadlocks_records --iterations 1"

        query = "SELECT id, `user`, `password`, `host`, `port` FROM sqlaudit_mysql_schemas " \
                "WHERE sqlaudit_mysql_schemas.is_master = 1 group by host,port"

        for row in MysqlSchemaInfo.objects.raw(query):
            format_command = command.format(user=row.user, password=row.password, host=row.host, port=row.port)
            if not DeadlockCommand.objects.filter(schema_id=row.id):
                print('www....')
                DeadlockCommand.objects.create(schema_id=row.id, command=format_command)

        # 轮询探测死锁
        for row in DeadlockCommand.objects.all():
            process = subprocess.Popen(row.command, shell=True)
            process.wait()
        # 检查进程是否启动，若没有，则启动进程
        # for row in DeadlockCommand.objects.filter(pid__gte=0):
        #     # 检查进程是否运行
        #     # 如果不存在，启动该进程
        #     if not psutil.pid_exists(row.pid):
        #         process = psutil.Popen(['/usr/bin/pt-deadlock-logger', row.command])
        #         DeadlockCommand.objects.filter(id=row.id).update(pid=process.pid, is_process_run=1)

        # 检查死锁，并发送报告
        i = 0
        step = 2
        result = []
        data = list(DeadlockRecord.objects.filter(is_pull=0).values())
        while i <= (len(data) - step):
            result.append({'data': [data[i], data[i + 1]]})
            i += step

        format_deadlock_data = ''
        j = 1
        for row in result:
            double_data = ''
            for i in row['data']:
                text = f"主机：{i['server']}\n" \
                       f"时间: {i['ts']}\n" \
                       f"线程ID: {i['thread']}\n" \
                       f"事务ID: {i['txn_id']}\n" \
                       f"事务激活时间: {i['txn_time']}\n" \
                       f"用户名: {i['user']}\n" \
                       f"主机名: {i['hostname']}\n" \
                       f"IP: {i['ip']}\n" \
                       f"库名: {i['db']}\n" \
                       f"表名: {i['tbl']} \n" \
                       f"发生死锁的索引: {i['idx']}\n" \
                       f"锁类型: {i['lock_type']}\n" \
                       f"锁模式: {i['lock_mode']}\n" \
                       f"请求锁: {i['wait_hold']}\n" \
                       f"是否回滚: {'否' if i['victim'] == 0 else '是'}\n" \
                       f"查询: {i['query']}\n\n"
                double_data += text
                DeadlockRecord.objects.filter(id=i['id']).update(is_pull=1)

            format_deadlock_data += ''.join((f'## 死锁记录{j} ##:\n', double_data))
            j += 1

        if result:
            check_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            xiaoding.send_text(msg='\n'.join((f'【警告 ◕﹏◕，探测到新的死锁记录，探测时间：{check_time}】\n', format_deadlock_data)),
                               is_at_all=True)