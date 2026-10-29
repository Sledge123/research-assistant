"""账号共用积分账本；由调用方的BEGIN IMMEDIATE事务保证跨功能原子结算。"""
from datetime import datetime,timedelta,timezone
import time
import uuid

SCHEMA='''
CREATE TABLE IF NOT EXISTS credit_entries (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),kind TEXT NOT NULL,
 amount INTEGER NOT NULL,frozen_amount INTEGER NOT NULL,event_key TEXT NOT NULL,
 job_id TEXT,created_at REAL NOT NULL,UNIQUE(user_id,event_key)
);
CREATE TABLE IF NOT EXISTS xrd_credit_holds (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id),user_id TEXT NOT NULL REFERENCES users(id),
 reserved INTEGER NOT NULL CHECK(reserved>0),charged INTEGER,
 CHECK(charged IS NULL OR charged BETWEEN 0 AND reserved)
);
'''
LABELS={'registration':'注册赠送','checkin':'每日签到','freeze':'文献求助冻结',
        'charge':'文献全文取得扣分','refund':'文献求助积分退回',
        'xrd_freeze':'XRD卡片归档冻结','xrd_charge':'XRD成功卡片扣分','xrd_refund':'XRD未成功卡片退回',
        'abtem_freeze':'电镜模拟冻结','abtem_charge':'电镜模拟结果扣分','abtem_refund':'电镜模拟积分退回',
        'pymatgen_freeze':'材料工具积分冻结','pymatgen_charge':'材料工具成功扣分','pymatgen_refund':'材料工具积分退回',
        'admin_adjustment':'管理员调整积分'}

class CreditError(ValueError):pass

class Credits:
    @staticmethod
    def today():return datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    @staticmethod
    def entry(db,user_id,kind,amount,frozen,event_key,job_id=None):
        db.execute('INSERT OR IGNORE INTO credit_entries VALUES (?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex,user_id,kind,amount,frozen,event_key,job_id,time.time()))
    @staticmethod
    def grant(db,user_id):
        from .debug_accounts import is_debug
        if is_debug(db,user_id):return
        if db.execute('SELECT 1 FROM users WHERE id=? AND verified=1',(user_id,)).fetchone():
            Credits.entry(db,user_id,'registration',10,0,'registration')
    @staticmethod
    def wallet(db,user_id,*,limit=200):
        values=db.execute('SELECT COALESCE(SUM(amount),0),COALESCE(SUM(frozen_amount),0) FROM credit_entries WHERE user_id=?',(user_id,)).fetchone()
        signed=db.execute('SELECT 1 FROM credit_entries WHERE user_id=? AND event_key=?',(user_id,'checkin:'+Credits.today())).fetchone()
        items=[]
        query='SELECT * FROM credit_entries WHERE user_id=? ORDER BY created_at DESC,rowid DESC'
        parameters=(user_id,) if limit is None else (user_id,limit)
        for row in db.execute(query+(' LIMIT ?' if limit is not None else ''),parameters):
            items.append({'id':row['id'],'kind':row['kind'],'label':LABELS.get(row['kind'],row['kind']),
                          'amount':row['amount'],'frozen_amount':row['frozen_amount'],
                          'created_at':datetime.fromtimestamp(row['created_at'],timezone.utc).isoformat(),
                          'job_id':row['job_id'],'job_kind':('xrd' if row['kind'].startswith('xrd_') else 'abtem' if row['kind'].startswith('abtem_') else 'pymatgen' if row['kind'].startswith('pymatgen_') else 'literature') if row['job_id'] else None})
        return {'available':values[0],'frozen':values[1],'checked_in':bool(signed),'items':items}
    @staticmethod
    def adjust(db,user_id,identifier,amount):
        if type(amount) is not int or amount==0 or abs(amount)>1000000:
            raise CreditError('管理员积分调整数量无效。')
        available=db.execute('SELECT COALESCE(SUM(amount),0) FROM credit_entries WHERE user_id=?',(user_id,)).fetchone()[0]
        if available+amount<0:
            raise CreditError('可用积分不足，不能扣除冻结积分，请刷新余额后重试。')
        db.execute('INSERT INTO credit_entries VALUES (?,?,?,?,?,?,NULL,?)',
                   (identifier,user_id,'admin_adjustment',amount,0,'admin_adjustment:'+identifier,time.time()))
    @staticmethod
    def reserve_xrd(db,user_id,job_id,count):
        Credits.grant(db,user_id)
        if Credits.wallet(db,user_id)['available']<count:
            raise CreditError(f'可用积分不足，本次需冻结{count}积分；每张卡片成功扣1分，失败退回。')
        db.execute('INSERT INTO xrd_credit_holds VALUES (?,?,?,NULL)',(job_id,user_id,count))
        Credits.entry(db,user_id,'xrd_freeze',-count,count,'xrd:freeze:'+job_id,job_id)
    @staticmethod
    def settle_xrd(db,job_id,successful):
        row=db.execute('SELECT * FROM xrd_credit_holds WHERE job_id=?',(job_id,)).fetchone()
        # 上线前任务不补扣；已经结算的回调/取消不得重复操作账本。
        if not row or row['charged'] is not None:return
        if type(successful) is not int or not 0<=successful<=row['reserved']:raise CreditError('卡片结算数量无效。')
        unused=row['reserved']-successful
        if successful:Credits.entry(db,row['user_id'],'xrd_charge',0,-successful,'xrd:charge:'+job_id,job_id)
        if unused:Credits.entry(db,row['user_id'],'xrd_refund',unused,-unused,'xrd:refund:'+job_id,job_id)
        db.execute('UPDATE xrd_credit_holds SET charged=? WHERE job_id=?',(successful,job_id))
    @staticmethod
    def xrd_status(db,job_id):
        row=db.execute('SELECT reserved,charged FROM xrd_credit_holds WHERE job_id=?',(job_id,)).fetchone()
        if not row:return {'reserved':0,'charged':0,'status':'not_required'}
        charged=row['charged']
        return {'reserved':row['reserved'],'charged':charged or 0,
                'status':'frozen' if charged is None else ('charged' if charged==row['reserved'] else 'partial' if charged else 'refunded')}
