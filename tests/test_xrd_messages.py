import unittest


class XrdMessageTests(unittest.TestCase):
    def message(self,**values):
        from highscore_web.xrd_messages import public_message
        row={'kind':'archive','state':'needs_attention','archive_status':'failed','mail_status':'not_requested',
             'message':'内部路径、指纹或运行报错',**values}
        return public_message(row,values.get('results',[]),values.get('credit',{}))

    def test_all_missing_reports_only_business_result_and_actual_refund(self):
        values={'results':[{'code':'09-999-0001','status':'not_found'}]}
        self.assertEqual(self.message(**values),'未发现该卡片信息')
        self.assertEqual(self.message(**values,credit={'reserved':1,'charged':0,'status':'refunded'}),
                         '未发现该卡片信息，已返还1积分。')

    def test_private_progress_and_historical_diagnostics_do_not_escape(self):
        self.assertEqual(self.message(state='running'),'正在处理')
        self.assertEqual(self.message(state='queued'),'排队中')
        self.assertEqual(self.message(),'任务未完成，请重试或联系管理员。')

    def test_partial_result_and_uncertain_mail_remain_actionable(self):
        value=self.message(archive_status='partial',results=[{'code':'00-001-0001','status':'success'},
                          {'code':'00-001-0002','status':'not_found'}],credit={'reserved':2,'charged':1,'status':'partial'})
        self.assertIn('未发现该卡片信息：00-001-0002',value)
        self.assertIn('已返还1积分',value)
        self.assertIn('请先核对收件箱',self.message(archive_status='success',mail_status='unknown'))

    def test_too_broad_search_explains_user_action_without_internal_diagnostics(self):
        self.assertEqual(self.message(kind='search',message='检索结果超过 100 条，请细化条件；未截断为完整结果。'),
                         '匹配结果过多，请细化检索条件。')
