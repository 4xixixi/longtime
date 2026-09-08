import unittest
from control import supervisor_ctl as ctl
from tests import test_handler_handoff
from tests.test_supervisor_ctl import write_json


class UserRetryTests(unittest.TestCase):
    def case(self):
        case = test_handler_handoff.HandoffTests()
        case.setup_case(resume=True)
        self.addCleanup(case.doCleanups)
        case.commit()
        return case

    def request(self, case, role='USER', **changes):
        run = ctl.acquire_lease(case.root, role, None, None)
        req = {'reason':'用户明确要求立即恢复原任务','expected':run['expected'],'patches':{},
               'handler_retry':{'event_id':case.env['event_id'],'thread_id':'original','authorization_basis':'帮我恢复'},
               'finish':{'outcome':'HANDLER_RETRY_AUTHORIZED'}}
        req.update(changes)
        path=case.root/'retry.json';write_json(path,req)
        return ctl.commit_request(case.root,run['lease_token'],path)

    def test_user_retry_is_one_shot_and_preserves_event_and_budget(self):
        case=self.case()
        before=ctl.read_json(case.root/'jobs/job-one/state.json')
        self.assertEqual(ctl.check(case.root)['next_action']['type'],'NO_ACTION')
        self.assertEqual(self.request(case)['report']['visibility'],'PROGRESS')
        self.assertEqual(ctl.check(case.root)['next_action']['type'],'SOL_STATUS')
        after=ctl.read_json(case.root/'jobs/job-one/state.json')
        for key in ['semantic_revision','event_generation','escalation_metrics']:
            self.assertEqual(before['controller'][key],after['controller'][key])
        self.assertEqual(before['controller']['current_event']['event_id'],after['controller']['current_event']['event_id'])
        run=ctl.acquire_lease(case.root,'LUNA',None,None)
        path=case.root/'observed.json'
        write_json(path,{'reason':'observe original','expected':run['expected'],'patches':{},
                         'event_update':{'thread_status':'active','status_checked_at':ctl.iso_utc()},'finish':{'outcome':'SOL_RUNNING'}})
        ctl.commit_request(case.root,run['lease_token'],path)
        self.assertEqual(ctl.check(case.root)['next_action']['type'],'NO_ACTION')

    def test_luna_cannot_authorize_its_own_retry(self):
        with self.assertRaisesRegex(ctl.ControlError,'REQUIRES_USER'):
            self.request(self.case(),role='LUNA')

    def test_wrong_thread_or_unrelated_patches_are_rejected(self):
        for changes in ({'handler_retry':{'event_id':'wrong','thread_id':'other','authorization_basis':'yes'}},
                        {'patches':{'job':{'status':'QUEUED'}}}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ctl.ControlError,'IDENTITY_OR_SCOPE'):
                self.request(self.case(),**changes)


if __name__=='__main__': unittest.main()
