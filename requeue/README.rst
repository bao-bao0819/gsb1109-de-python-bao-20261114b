requeue
=======

把一次断连后的采集作业重新排队时，不能靠“看起来没发完”来猜。
本包读一份**已持久化**的追加式发送台账，按已落盘的进度、请求身份和
源站**明确承诺**的幂等键规则，输出三份清单：

* ``resend``：可以立即重新排队；
* ``verify_remote``：已经暴露给源站，先核对远端回执（不要盲目建新作业）；
* ``delivered_local``：本地已有可交付产物或回执，不再重发。

用法
----

.. code-block:: bash

   python -m requeue journal.json --jobs

台账格式
--------

根对象可带 ``idempotency_rule`` 和 ``events``；也可直接传事件数组。

.. code-block:: json

   {
     "idempotency_rule": {
       "explicit": true,
       "source": "export API guide v3 section 4.2",
       "methods": ["POST"],
       "same_key_same_body_returns_same_receipt": true,
       "different_body_same_key": "isolate"
     },
     "events": [
       {"event_id": "e1", "kind": "request_registered", "request_id": "r1",
        "data": {"method": "POST", "url": "/exports",
                 "body_sha256": "sha256:...", "idempotency_key": "k-1"}},
       {"event_id": "e2", "kind": "attempt_headers_sent",
        "request_id": "r1", "attempt_id": "a1", "data": {"body_total": 8192}},
       {"event_id": "e3", "kind": "attempt_body_progress",
        "request_id": "r1", "attempt_id": "a1", "data": {"bytes": 1024}}
     ]
   }

事件种类
~~~~~~~~

``request_registered``
  ``method`` / ``url`` / ``body_sha256`` / ``idempotency_key``；可选
  ``replay_of`` 指向同一逻辑请求的另一个 id（仅用于显式合并重放别名）。
``attempt_headers_sent``
  请求行和头部已经交给传输层，``body_total`` 可选。
``attempt_body_progress``
  ``bytes``（默认是本次新增的字节数；``cumulative: true`` 时是累计数），
  ``total`` 可选，``body_sha256`` 可选。
``attempt_body_complete``
  整个正文已送出。
``attempt_response``
  ``status``、``body_stored``、``receipt_id``。
``receipt_persisted`` / ``local_delivery``
  远端回执或本地产物已经落盘。

决策顺序
--------

1. 本地已交付（回执、本地产物、或已存储的成功响应）→ ``delivered_local``。
2. 幂等方法（``GET``/``HEAD``/``OPTIONS``/``TRACE``/``PUT``/``DELETE``）
   → ``resend``。可重复取的 GET 不受正文是否只送了一部分影响。
3. 没有任何字节交给传输层 → ``resend``（``no_exposure``）。
4. 其余 POST/创建类请求只要有过暴露 → 默认 ``verify_remote``。
   头部已送出、正文只送一部分仍算暴露，**不推定**源站没处理。
5. 仅在源站显式承诺“同键同正文返回同一回执”且已被 ``explicit`` 标记时，
   才回到 ``resend``（``explicit_idempotency_promise``）。
6. 同一个幂等键被不同正文复用 → 这些请求都进入 ``verify_remote``，
   并各带独立的 ``isolation_group``，回执不能互相归因。

保证
----

* ``event_id`` 是至少一次投递的去重键：同 id 同内容的重放被忽略，同 id
  不同内容会记入 ``anomalies``；``supplement_jobs`` 按
  ``(request_id, attempt_id)`` 去重，因此重放同一段事件不会产生第二个补发作业。
* 同一键 + 同一正文的两条请求记录会合并成一个逻辑请求，只产生一个作业；
  键相同但正文不同的请求永不合并。
* 未记录正文哈希时，两个同 URL 的 POST 不会被当成同一请求而吞掉其中一个导出作业。
