"""新版免费编号/化学式检索与完整候选快照的离线契约。"""
import unittest

import test_web_api

BASE = test_web_api.BASE


class BrowseSearchApiTests(unittest.TestCase):
    def setUp(self):
        self.h = test_web_api.WebApiTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.user = self.h.register()
        self.h.enable(self.user)
        self.headers = {'X-CSRF-Token': self.user['csrf_token']}

    def search(self, mode='formula', query='MnO2', key='browse', **extra):
        return self.h.client.post(BASE + '/jobs/search', headers=self.headers,
            json={'mode': mode, 'query': query, 'request_key': key, 'search_version': 2, **extra})

    @staticmethod
    def candidates(count=121):
        return [{'code': f'09-999-{index:04d}', 'phase_name': 'Synthetic manganese oxide',
            'chemical_formula': 'Mn O2', 'source': 'Synthetic source',
            'mineral_name': 'Synthetic mineral', 'space_group': 'P 1',
            'cell_parameters': {'a': '3.1415927', 'b': 4.0, 'c': None, 'alpha': '90'},
            'peak_count': 2, 'content_type': 'diffraction', 'exportable': True}
            for index in range(1, count + 1)]

    def finished(self, candidates=None):
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.h.client.get(BASE + '/jobs/' + response.json()['id'] + '/candidates').status_code, 409)
        claim = self.h.claim().json()
        done = self.h.complete(claim, result={'candidates': self.candidates() if candidates is None else candidates})
        self.assertEqual(done.status_code, 200, done.text)
        return response.json()['id'], claim

    def test_safe_search_context_and_download_parent_are_returned_for_page_restore(self):
        response = self.search(query='mno2', key='context-search', content='diffraction')
        self.assertEqual(response.status_code, 200, response.text)
        search = response.json()
        self.assertEqual({key: search.get(key) for key in ('search_query','search_mode','content_type')},
                         {'search_query':'mno2','search_mode':'formula','content_type':'diffraction'})
        claim = self.h.claim().json()
        self.assertEqual(self.h.complete(claim, result={'candidates':self.candidates(2)}).status_code, 200)
        search = self.h.client.get(BASE + '/jobs/' + search['id']).json()
        archive = self.h.client.post(BASE + '/jobs/archive', headers=self.headers,
            json={'search_job_id':search['id'],'candidate_ids':[search['candidates'][0]['id']], 'request_key':'context-download'})
        self.assertEqual(archive.status_code, 200, archive.text)
        self.assertEqual(archive.json().get('search_job_id'), search['id'])
        import json
        with self.h.db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (search['id'],)).fetchone()
            payload = json.loads(row['payload']);payload['private_probe']='synthetic-private-value'
            db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(payload), search['id']))
        jobs = self.h.client.get(BASE + '/jobs').json()['items']
        for job in jobs:
            self.assertNotIn('payload', job)
            self.assertNotIn('private_probe', json.dumps(job))
        restored = next(job for job in jobs if job['id']==search['id'])
        self.assertEqual(restored['search_query'], 'mno2')
        child = next(job for job in jobs if job['id']==archive.json()['id'])
        self.assertEqual(child['search_job_id'], search['id'])

    def test_twenty_full_codes_keep_original_query_after_reload(self):
        query = ' '.join(f'09-999-{index:04d}' for index in range(1, 21))
        self.assertGreater(len(query), 200)
        response = self.search('codes', query, key='context-twenty-codes')
        self.assertEqual(response.status_code, 200, response.text)
        job = self.h.client.get(BASE + '/jobs/' + response.json()['id']).json()
        self.assertEqual(job['search_query'], query)
        self.assertEqual(job['search_mode'], 'codes')
        listed = self.h.client.get(BASE + '/jobs').json()['items']
        self.assertEqual(next(item for item in listed if item['id']==job['id'])['search_query'], query)

    def test_codes_search_is_free_and_never_creates_archive_or_mail(self):
        before = self.h.client.get(BASE + '/auth/me').json()['credits']
        response = self.search('codes', '19-0629\nICSD-1')
        self.assertEqual(response.status_code, 200, response.text)
        claimed = self.h.claim().json()
        self.assertEqual(claimed['job']['payload'], {'mode': 'codes', 'query': '19-0629\nICSD-1', 'search_version': 2})
        self.assertEqual(self.h.complete(claimed, result={'candidates': []}).status_code, 200)
        after = self.h.client.get(BASE + '/auth/me').json()['credits']
        self.assertEqual(before, after)
        with self.h.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM xrd_credit_holds').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='archive'").fetchone()[0], 0)

    def test_every_candidate_can_be_paged_without_new_jobs_or_credit_changes(self):
        job_id, claim = self.finished()
        view = self.h.client.get(BASE + '/jobs/' + job_id).json()
        self.assertEqual(view['candidate_total'], 121)
        self.assertEqual(view['candidate_next_offset'], 50)
        self.assertEqual(len(view['candidates']), 50)
        self.assertEqual(view['search_version'], 2)
        self.assertEqual(view['candidates'][0]['cell_parameters']['a'], '3.1415927')
        self.assertEqual(view['candidates'][0]['mineral_name'], 'Synthetic mineral')
        before = self.h.client.get(BASE + '/auth/me').json()
        collected, offset = [], 0
        while offset is not None:
            response = self.h.client.get(BASE + f'/jobs/{job_id}/candidates', params={'offset': offset, 'limit': 50})
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            self.assertEqual(page['total'], 121)
            collected.extend(page['items'])
            offset = page['next_offset']
        self.assertEqual(len(collected), 121)
        self.assertEqual(len({item['id'] for item in collected}), 121)
        self.assertEqual(collected[:50], view['candidates'])
        self.assertEqual([x['code'] for x in collected], [x['code'] for x in self.candidates()])
        self.assertEqual(self.h.client.get(BASE + '/auth/me').json(), before)
        with self.h.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candidate_details').fetchone()[0], 121)
        # 旧租约不能重放，从而重复插入同一完整快照。
        self.assertEqual(self.h.complete(claim, result={'candidates': self.candidates()}).status_code, 409)

    def test_late_page_selection_keeps_source_and_only_then_reserves_one_point(self):
        job_id, _ = self.finished()
        page = self.h.client.get(BASE + f'/jobs/{job_id}/candidates', params={'offset': 100}).json()
        chosen = page['items'][0]
        response = self.h.client.post(BASE + '/jobs/archive', headers=self.headers,
            json={'search_job_id': job_id, 'candidate_ids': [chosen['id']], 'request_key': 'choose-late'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['credits']['reserved'], 1)
        claimed = self.h.claim().json()['job']
        self.assertEqual(claimed['payload']['selected'][0],
            {'code': chosen['code'], 'source': chosen['source'], 'candidate_id': chosen['id']})
        self.assertFalse(claimed['payload']['send_mail'])

    def test_invalid_modes_query_lengths_and_pagination_are_rejected(self):
        for mode in ('compound_name', 'mineral_name'):
            self.assertEqual(self.search(mode, key=mode).status_code, 422)
        self.assertEqual(self.search(query='X' * 201).status_code, 422)
        self.assertEqual(self.search('codes', 'invalid-card').status_code, 422)
        self.assertEqual(self.search('codes', '19-0629\x01').status_code, 422)
        legacy_codes = self.h.client.post(BASE + '/jobs/search', headers=self.headers,
            json={'mode': 'codes', 'query': '19-0629', 'request_key': 'legacy-codes'})
        self.assertEqual(legacy_codes.status_code, 422)
        job_id, _ = self.finished([])
        for params in ({'offset': -1}, {'offset': 10**50}, {'limit': 0}, {'limit': 51}, {'offset': 'oops'}, {'limit': 'oops'}):
            self.assertEqual(self.h.client.get(BASE + f'/jobs/{job_id}/candidates', params=params).status_code, 422)
        page = self.h.client.get(BASE + f'/jobs/{job_id}/candidates', params={'offset': 1000}).json()
        self.assertEqual(page, {'items': [], 'total': 0, 'next_offset': None})
        job = self.h.client.get(BASE + '/jobs/' + job_id).json()
        self.assertEqual(job['message'], '未发现该卡片信息')
        self.assertEqual(job['credits']['reserved'], 0)

    def test_duplicate_or_unknown_metadata_rolls_back_the_complete_snapshot(self):
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        claim = self.h.claim().json()
        candidates = self.candidates(2)
        bad = [candidates[0], {**candidates[1], 'internal_path': 'synthetic-private'}]
        self.assertEqual(self.h.complete(claim, result={'candidates': bad}).status_code, 422)
        bad = [candidates[0], {**candidates[1], 'cell_parameters': {'unknown_axis': '5'}}]
        self.assertEqual(self.h.complete(claim, result={'candidates': bad}).status_code, 422)
        bad = [candidates[0], {**candidates[0]}]
        self.assertEqual(self.h.complete(claim, result={'candidates': bad}).status_code, 422)
        with self.h.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM candidate_details').fetchone()[0], 0)
        self.assertEqual(self.h.complete(claim, result={'candidates': candidates}).status_code, 200)

    def test_cross_user_pages_and_selection_are_forbidden(self):
        job_id, _ = self.finished(self.candidates(1))
        candidate = self.h.client.get(BASE + '/jobs/' + job_id).json()['candidates'][0]
        self.h.client.post(BASE + '/auth/logout', headers=self.headers)
        other = self.h.register('other-browse@example.com')
        headers = {'X-CSRF-Token': other['csrf_token']}
        self.assertEqual(self.h.client.get(BASE + f'/jobs/{job_id}/candidates').status_code, 404)
        response = self.h.client.post(BASE + '/jobs/archive', headers=headers,
            json={'search_job_id': job_id, 'candidate_ids': [candidate['id']], 'request_key': 'forged'})
        self.assertEqual(response.status_code, 404)

    def test_legacy_limit_and_existing_candidate_shape_remain_unchanged(self):
        response = self.h.client.post(BASE + '/jobs/search', headers=self.headers,
            json={'mode': 'formula', 'query': 'X Y', 'request_key': 'legacy'})
        self.assertEqual(response.status_code, 200, response.text)
        claim = self.h.claim().json()
        candidates = [{key: item[key] for key in ('code', 'phase_name', 'chemical_formula', 'source')}
                      for item in self.candidates(101)]
        self.assertEqual(self.h.complete(claim, result={'candidates': candidates}).status_code, 422)
        self.assertEqual(self.h.complete(claim, result={'candidates': candidates[:1]}).status_code, 200)
        view = self.h.client.get(BASE + '/jobs/' + response.json()['id']).json()
        self.assertNotIn('search_version', view)
        self.assertNotIn('candidate_total', view)
        self.assertEqual(set(view['candidates'][0]), {'id', 'code', 'phase_name', 'chemical_formula', 'source'})

    def test_same_code_different_sources_remain_distinct_but_one_archive_source_per_code(self):
        candidates = self.candidates(1)
        candidates.append({**candidates[0], 'source': 'Synthetic second source'})
        job_id, _ = self.finished(candidates)
        items = self.h.client.get(BASE + f'/jobs/{job_id}/candidates').json()['items']
        self.assertEqual(len(items), 2)
        response = self.h.client.post(BASE + '/jobs/archive', headers=self.headers,
            json={'search_job_id': job_id, 'candidate_ids': [x['id'] for x in items], 'request_key': 'ambiguous'})
        self.assertEqual(response.status_code, 409)
        response = self.h.client.post(BASE + '/jobs/archive', headers=self.headers,
            json={'search_job_id': job_id, 'candidate_ids': [items[1]['id']], 'request_key': 'second-source'})
        self.assertEqual(response.status_code, 200, response.text)
        payload = self.h.claim().json()['job']['payload']
        self.assertEqual(payload['selected'][0]['source'], 'Synthetic second source')
        self.assertEqual(payload['search_version'], 2)


if __name__ == '__main__':
    unittest.main()
