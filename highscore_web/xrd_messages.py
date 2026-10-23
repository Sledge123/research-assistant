"""用户可读状态；数据库仍保存执行器原始说明供管理核查。"""


def public_message(row,results,credit):
    state=row['state']
    if state=='queued':return '排队中'
    if state=='running':return '正在检索' if row['kind']=='search' else '正在处理'
    if state=='cancelled':return '任务已取消'
    if row['kind']=='search':
        if state=='succeeded':return '检索完成' if results else '未发现该卡片信息'
        if row['message']=='检索结果超过 100 条，请细化条件；未截断为完整结果。':
            return '匹配结果过多，请细化检索条件。'
        return '检索未完成，请重试或联系管理员。'
    if row['kind']=='mail_retry':
        return {'submitted':'邮件已提交','unknown':'邮件状态待确认，请先核对收件箱。',
                'failed':'邮件发送失败，可重新发送。'}.get(row['mail_status'],'邮件尚未发送')
    missing=[item['code'] for item in results if item.get('status')=='not_found']
    refunded=max(0,credit.get('reserved',0)-credit.get('charged',0)) if credit.get('status') in {'charged','partial','refunded'} else 0
    if missing and len(missing)==len(results):
        return '未发现该卡片信息'+(f'，已返还{refunded}积分。' if refunded else '')
    message={'success':'报告已生成','partial':'部分报告已生成'}.get(row['archive_status'],
                '任务未完成，请重试或联系管理员。')
    if missing:message+='。未发现该卡片信息：'+'、'.join(missing)+'。'
    if refunded:message+=f'已返还{refunded}积分。'
    if row['mail_status']=='unknown':message+=' 邮件状态待确认，请先核对收件箱。'
    elif row['mail_status']=='failed':message+=' 邮件发送失败，可重新发送。'
    return message


def public_card_results(results):
    messages={'not_found':'未发现该卡片信息','failed':'卡片处理失败',
              'not_started':'未处理','cancelled':'已取消','needs_attention':'卡片处理未完成'}
    return [{**item,'message':messages.get(item.get('status'),'')} if 'message' in item else dict(item)
            for item in results]
