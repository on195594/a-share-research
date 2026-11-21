-- Successful production runs. Only the columns read by the evaluation report were captured.
CREATE TABLE runs (id INTEGER PRIMARY KEY, company_key TEXT, status TEXT, started_at TEXT, finished_at TEXT);
CREATE TABLE calls (id INTEGER PRIMARY KEY, run_id INTEGER, step TEXT, seconds REAL, usage TEXT);
INSERT INTO runs VALUES
    (243,'002648','done','2026-11-19T08:06:39+08:00','2026-11-19T08:10:40+08:00'),
    (245,'601600','done','2026-11-19T10:33:33+08:00','2026-11-19T10:41:46+08:00'),
    (246,'002648','done','2026-11-19T10:41:46+08:00','2026-11-19T10:45:33+08:00');
INSERT INTO calls VALUES
    (1692,243,'explore',7.54828275999170728e+00,'{"input":23348,"output":208,"cacheRead":1792}'),
    (1693,243,'explore',8.57150383997941389e+00,'{"input":5025,"output":261,"cacheRead":24832}'),
    (1694,243,'explore',1.08245340800203848e+01,'{"input":8709,"output":369,"cacheRead":29696}'),
    (1695,243,'explore',7.34779135999269783e+00,'{"input":9511,"output":208,"cacheRead":38144}'),
    (1696,243,'explore',9.55473271998926,'{"input":1775,"output":234,"cacheRead":47360}'),
    (1697,243,'explore',1.49938600119989132e+02,'{"input":2072,"output":6687,"cacheRead":48896}'),
    (1698,243,'review',3.57203625600086525e+01,'{"input":50870,"output":986,"cacheRead":0}'),
    (1708,245,'explore',5.91631568002048879e+00,'{"input":28312,"output":80,"cacheRead":0}'),
    (1709,245,'explore',1.02886677999631501e+01,'{"input":2121,"output":243,"cacheRead":28160}'),
    (1710,245,'explore',1.07773605199763551e+01,'{"input":10025,"output":276,"cacheRead":29952}'),
    (1711,245,'explore',1.07120981600019149e+01,'{"input":8702,"output":257,"cacheRead":39680}'),
    (1712,245,'explore',1.33636819200473837e+01,'{"input":5984,"output":342,"cacheRead":48128}'),
    (1713,245,'explore',1.43346915279980748e+02,'{"input":2396,"output":6230,"cacheRead":53760}'),
    (1714,245,'review',4.99190318399923853e+01,'{"input":55531,"output":1850,"cacheRead":0}'),
    (1715,245,'repair',4.96793819998856633e+00,'{"input":57917,"output":105,"cacheRead":0}'),
    (1716,245,'repair',6.4021374839998316e+01,'{"input":3735,"output":2758,"cacheRead":57600}'),
    (1717,245,'repair',5.70035284000914543e+00,'{"input":62219,"output":111,"cacheRead":0}'),
    (1718,245,'repair',2.29461056800209917e+01,'{"input":779,"output":350,"cacheRead":61952}'),
    (1719,245,'repair',6.45820831199525855e+01,'{"input":1016,"output":2806,"cacheRead":62464}'),
    (1720,245,'review',67.2227503999602,'{"input":63148,"output":3189,"cacheRead":0}'),
    (1721,246,'explore',4.62480147997848689e+00,'{"input":25255,"output":19,"cacheRead":0}'),
    (1722,246,'explore',9.60312144004274159e+00,'{"input":829,"output":303,"cacheRead":25088}'),
    (1723,246,'explore',8.52775143994949758e+00,'{"input":5385,"output":199,"cacheRead":25600}'),
    (1724,246,'explore',1.09895416000508703e+01,'{"input":8788,"output":321,"cacheRead":30720}'),
    (1725,246,'explore',7.6884295999770984e+00,'{"input":10188,"output":220,"cacheRead":39168}'),
    (1726,246,'explore',7.93668063997756689e+00,'{"input":1129,"output":189,"cacheRead":49152}'),
    (1727,246,'explore',1.26723972919979132e+02,'{"input":2130,"output":5571,"cacheRead":50176}'),
    (1728,246,'review',3.82471609200001694e+01,'{"input":50628,"output":1110,"cacheRead":0}');
