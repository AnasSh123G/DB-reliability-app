import numpy as np
import pytest
from types import SimpleNamespace
torch = pytest.importorskip("torch")
from recency_weights import policy, weights, per_arrival_loss, weighted_loss
from dbrel.ml.arrival_nn import arrival_loss


def test_calendar_policy_and_endpoints():
    r=policy('2026-08-22')
    assert r['start']=='2026-07-22'
    days=np.array(['2026-07-01','2026-07-22','2026-08-06','2026-08-22'],dtype='datetime64[D]').astype('int64')
    np.testing.assert_allclose(weights(days,r),[1,1,1+.5*15/31,1.5])
    assert policy('2026-03-31')['start']=='2026-02-28'
    assert policy('2024-03-31')['start']=='2024-02-29'
    assert policy('2026-01-31')['start']=='2025-12-31'
    with pytest.raises(ValueError):weights(np.array([int(np.datetime64('2026-08-23','D').astype(int))]),r)


@pytest.mark.parametrize('head',['quantile','binned'])
@pytest.mark.parametrize('members',[1,3])
def test_unreduced_loss_preserves_original_and_weighting(head,members):
    torch.manual_seed(9)
    cfg=SimpleNamespace(head=head)
    raw=torch.randn(7,3 if head=='quantile' else 5,requires_grad=True)
    if members>1:raw=torch.randn(7,members,raw.shape[-1],requires_grad=True)
    y=torch.arange(7,dtype=torch.float32)-2;yb=torch.arange(7)%5
    per=per_arrival_loss(cfg,raw,y,yb)
    torch.testing.assert_close(per.mean(),arrival_loss(cfg,raw,y,yb))
    torch.testing.assert_close(weighted_loss(cfg,raw,y,yb,torch.ones(7)),arrival_loss(cfg,raw,y,yb))
    w=torch.tensor([1,1,1,1.1,1.2,1.3,1.5])
    actual=weighted_loss(cfg,raw,y,yb,w)
    torch.testing.assert_close(actual,(per*w).sum()/w.sum())
    p=torch.tensor([6,3,1,5,0,4,2])
    torch.testing.assert_close(weighted_loss(cfg,raw[p],y[p],yb[p],w[p]),actual)
    actual.backward();assert torch.isfinite(raw.grad).all()
    with pytest.raises(ValueError):weighted_loss(cfg,raw,y,yb,torch.zeros(7))


def test_weighted_cpu_training_resume_and_policy_rejection(tmp_path,monkeypatch):
    from test_compact_training import _tiny_net_and_data,_validation_data,_state_equal
    from dbrel.ml.arrival_nn import BatchSource
    import train_recency_delay as runner
    torch.set_num_threads(1)
    net,cats,nums,y=_tiny_net_and_data()
    data=_validation_data(tmp_path/'val',cats,nums,y)
    class WeightedBatch(BatchSource):
        def blocks(self,rng=None):
            spans=self.spans();order=rng.permutation(len(spans)) if rng is not None else range(len(spans))
            for i in order:
                a,b=spans[i]
                yield (*self.read(a,b),np.linspace(1,1.5,len(y),dtype=np.float32)[a:b])
    source=WeightedBatch(cats,nums,y,block_rows=7)
    key={'recency':policy('2026-08-22')}
    full=tmp_path/'full';full.mkdir()
    h,best=runner.training(net,source,data,len(y),'cpu',full,key,None)
    resumed,*_=_tiny_net_and_data();directory=tmp_path/'resume';directory.mkdir()
    def stop(message):
        if message.startswith('epoch 1:'):raise RuntimeError('interrupt')
    log=runner.log;monkeypatch.setattr(runner,'log',stop)
    with pytest.raises(RuntimeError,match='interrupt'):runner.training(resumed,source,data,len(y),'cpu',directory,key,None)
    with pytest.raises(ValueError,match='setup changed'):
        runner.training(resumed,source,data,len(y),'cpu',directory,{'recency':policy('2026-08-22',.6)},None)
    monkeypatch.setattr(runner,'log',log)
    rh,rb=runner.training(resumed,source,data,len(y),'cpu',directory,key,None)
    assert rb==best
    assert [{k:v for k,v in x.items() if k!='seconds'} for x in rh]==[{k:v for k,v in x.items() if k!='seconds'} for x in h]
    _state_equal(net.model.state_dict(),resumed.model.state_dict())


def test_weighted_source_keeps_dates_with_shuffled_blocks(tmp_path,monkeypatch):
    import train_recency_delay as runner
    from dbrel.ml.full_year import FileColumn
    monkeypatch.setattr(runner,'BLOCK_ROWS',3)
    n=8;days=np.arange(int(np.datetime64('2026-08-15','D').astype(int)),int(np.datetime64('2026-08-15','D').astype(int))+n,dtype=np.int32)
    arrays={'cats':np.zeros((n,8),np.int32),'nums':np.arange(n,dtype=np.float32)[:,None],'y':np.arange(n,dtype=np.float32),'day':days}
    columns={}
    for name,a in arrays.items():
        p=tmp_path/f'{name}.bin';a.tofile(p);columns[name]=FileColumn(p,a.dtype,n,a.shape[1] if a.ndim==2 else 0)
    class Data:
        cats=columns['cats'];nums=columns['nums'];y=columns['y']
        def column(self,name):return columns[name]
    r=policy('2026-08-22');source=runner.WeightedSource(Data(),r)
    try:
        seen=[]
        for cats,nums,y,w in source.blocks(np.random.default_rng(9)):
            ids=y.astype(int);seen.extend(ids.tolist());np.testing.assert_array_equal(w,weights(days[ids],r));np.testing.assert_array_equal(nums[:,0],y)
        assert sorted(seen)==list(range(n))
    finally:source.close()
