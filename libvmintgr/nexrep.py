# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import sys
import csv
import StringIO
import datetime
import pytz
from texttable import *

import debug
import vuln
import nexadhoc

OUTMODE_ASCII = 0
OUTMODE_CSV = 1

device_filter = None
filter_dupip = False
report_outmode = OUTMODE_ASCII

class VMDataSet(object):
    def __init__(self):
        self.current_state = None
        self.current_statestat = {}
        self.current_compliance = None
        self.current_compstat = {}

        self.previous_states = []
        self.previous_statestat = []
        self.previous_compliance = []
        self.previous_compstat = []

        self.hist = None

def set_report_mode(m):
    global report_outmode
    report_outmode = m

def populate_query_filters(scanner, gid):
    populate_device_filter(scanner, gid)

def populate_device_filter(scanner, gid):
    global device_filter

    squery = '''
    SELECT asset_id FROM dim_asset_group_asset
    WHERE asset_group_id = %s
    ''' % gid

    debug.printd('populating device filter...')
    buf = nexadhoc.nexpose_adhoc(scanner, squery, [], api_version='1.3.2')
    device_filter = []
    reader = csv.reader(StringIO.StringIO(buf))
    for i in reader:
        if i == None or len(i) == 0:
            continue
        if i[0] == 'asset_id':
            continue
        if i[0] not in device_filter:
            device_filter.append(i[0])
    debug.printd('%d devices in device filter' % len(device_filter))

def vulns_over_time(scanner, gid, start, end):
    squery = '''
    WITH applicable_assets AS (
    SELECT asset_id FROM dim_asset_group_asset
    WHERE asset_group_id = %s
    ),
    applicable_scans AS (
    SELECT asset_id, scan_id
    FROM fact_asset_scan
    WHERE (scan_finished >= '%s') AND
    (scan_finished <= '%s') AND
    asset_id IN (SELECT asset_id FROM applicable_assets)
    ),
    all_findings AS (
    SELECT fasvf.asset_id, da.ip_address, da.host_name,
    MIN(fasvf.date) as first_seen,
    MAX(fasvf.date) as last_seen, fasvf.vulnerability_id,
    dv.title AS vulnerability,
    round(dv.cvss_score::numeric, 2) AS cvss_score
    FROM fact_asset_scan_vulnerability_finding fasvf
    JOIN dim_asset da USING (asset_id)
    JOIN dim_vulnerability dv USING (vulnerability_id)
    JOIN applicable_scans USING (asset_id, scan_id)
    GROUP BY asset_id, ip_address, host_name, vulnerability_id,
    vulnerability, cvss_score
    )
    SELECT * FROM all_findings
    ''' % (gid, start, end)

    ret = nexadhoc.nexpose_adhoc(scanner, squery, [], api_version='1.3.2',
        device_ids=device_filter)
    reader = csv.reader(StringIO.StringIO(ret))
    vulnret = {}
    cnt = 0
    for i in reader:
        if i == None or len(i) == 0:
            continue
        if i[0] == 'asset_id':
            continue
        newvuln = vuln.vulnerability()
        newvuln.assetid = int(i[0])
        newvuln.ipaddr = i[1]
        newvuln.hostname = i[2]
        newvuln.vid = i[5]
        newvuln.title = i[6]
        newvuln.cvss = float(i[7])

        idx = i[3].find('.')
        if idx > 0:
            dstr = i[3][:idx]
        else:
            dstr = i[3]
        dt = datetime.datetime.strptime(dstr, '%Y-%m-%d %H:%M:%S')
        dt = dt.replace(tzinfo=pytz.UTC)
        first_date = dt

        idx = i[4].find('.')
        if idx > 0:
            dstr = i[4][:idx]
        else:
            dstr = i[4]
        dt = datetime.datetime.strptime(dstr, '%Y-%m-%d %H:%M:%S')
        dt = dt.replace(tzinfo=pytz.UTC)
        last_date = dt

        if newvuln.assetid not in vulnret:
            vulnret[newvuln.assetid] = {}
        newfinding = {}
        newfinding['vulnerability'] = newvuln
        newfinding['first_date'] = first_date
        newfinding['last_date'] = last_date
        vulnret[newvuln.assetid][newvuln.vid] = newfinding
        cnt += 1

    debug.printd('vulns_over_time: returning %d issues from %s to %s' % \
        (cnt, start, end))
    return vulnret

def vulns_at_time(scanner, gid, timestamp):
    squery = '''
    WITH applicable_assets AS (
    SELECT asset_id FROM dim_asset_group_asset
    WHERE asset_group_id = %s
    ),
    asset_scan_map AS (
    SELECT asset_id, scanAsOf(asset_id, '%s') as scan_id
    FROM dim_asset
    WHERE asset_id IN (SELECT asset_id FROM applicable_assets)
    ),
    current_state_snapshot AS (
    SELECT
    fasvf.asset_id, da.ip_address, da.host_name,
    fasvf.date AS discovered_date,
    fasvf.vulnerability_id,
    dv.title AS vulnerability,
    round(dv.cvss_score::numeric, 2) AS cvss_score,
    dv.cvss_vector AS cvss_vector
    FROM fact_asset_scan_vulnerability_finding fasvf
    JOIN dim_asset da USING (asset_id)
    JOIN dim_vulnerability dv USING (vulnerability_id)
    JOIN asset_scan_map USING (asset_id, scan_id)
    WHERE fasvf.asset_id IN (SELECT asset_id FROM applicable_assets)
    ),
    issue_age AS (
    SELECT
    fasvf.asset_id, fasvf.vulnerability_id,
    MIN(fasvf.date) as earliest
    FROM fact_asset_scan_vulnerability_finding fasvf
    JOIN current_state_snapshot css USING (asset_id, vulnerability_id)
    GROUP BY asset_id, vulnerability_id
    )
    SELECT asset_id, ip_address, host_name, discovered_date,
    vulnerability_id, vulnerability, cvss_score, cvss_vector,
    iage.earliest,
    EXTRACT(EPOCH FROM (discovered_date - iage.earliest))
    FROM current_state_snapshot
    JOIN issue_age iage USING (asset_id, vulnerability_id)
    ''' % (gid, timestamp)

    ret = nexadhoc.nexpose_adhoc(scanner, squery, [], api_version='1.3.2',
        device_ids=device_filter)
    reader = csv.reader(StringIO.StringIO(ret))
    vulnret = {}
    cnt = 0
    duprem = 0
    for i in reader:
        if i == None or len(i) == 0:
            continue
        if i[0] == 'asset_id':
            continue
        newvuln = vuln.vulnerability()
        newvuln.assetid = int(i[0])
        newvuln.ipaddr = i[1]
        newvuln.hostname = i[2]

        if filter_dupip and \
            duplicate_test(vulnret, newvuln.assetid, newvuln.ipaddr):
            duprem += 1
            continue

        idx = i[3].find('.')
        if idx > 0:
            dstr = i[3][:idx]
        else:
            dstr = i[3]
        dt = datetime.datetime.strptime(dstr, '%Y-%m-%d %H:%M:%S')
        dt = dt.replace(tzinfo=pytz.UTC)
        newvuln.discovered_date = dt

        newvuln.vid = i[4]
        newvuln.title = i[5]
        newvuln.cvss = float(i[6])
        newvuln.cvss_vector = i[7]
        newvuln.age_days = float(i[9]) / 60 / 60 / 24

        if newvuln.assetid not in vulnret:
            vulnret[newvuln.assetid] = []
        vulnret[newvuln.assetid].append(newvuln)
        cnt += 1

    debug.printd('vulns_at_time: %s: returning %d issues for %d assets' % \
        (timestamp, cnt, len(vulnret.keys())))
    if duprem > 0:
        debug.printd('vulns_at_time: %d duplicate items were removed' % duprem)
    return vulnret

def duplicate_test(d, aid, ipaddr):
    for k in d:
        aent = d[k]
        if len(aent) == 0:
            continue
        if k == aid:
            continue
        if aent[0].ipaddr == ipaddr:
            return True
    return False

def vmd_compliance(vlist):
    # Create a compliance element for each finding in the list. failvuln
    # is used to point to the associated issue here, and the failed flag
    # is set on any failures.
    ret = []
    failcnt = 0
    for a in vlist:
        for v in vlist[a]:
            newcomp = vuln.ComplianceElement()
            newcomp.failed = False
            newcomp.failvuln = v
            for level in vuln.ComplianceLevels.ORDERING:
                if v.cvss >= vuln.ComplianceLevels.FLOOR[level] and \
                    v.age_days > vuln.ComplianceLevels.LEVELS[level]:
                    newcomp.failed = True
                    failcnt += 1
                    break
            ret.append(newcomp)
    debug.printd('vmd_compliance returning %d elements (%d failed)' \
        % (len(ret), failcnt))
    return ret

def compliance_impactsum(compset):
    ret = {}

    tmpbuf = {'maximum': {}, 'high': {}}
    for c in compset:
        if not c.failed:
            continue
        if c.failvuln.cvss < 7:
            continue
        elif c.failvuln.cvss >= 9:
            label = 'maximum'
        else:
            label = 'high'
        if c.failvuln.vid not in tmpbuf[label]:
            tmpbuf[label][c.failvuln.vid] = {'vid': c.failvuln.vid, \
                'count': 1, 'title': c.failvuln.title}
        else:
            tmpbuf[label][c.failvuln.vid]['count'] += 1
    ret['maximum'] = []
    for v in tmpbuf['maximum']:
        ret['maximum'].append(tmpbuf['maximum'][v])
    ret['high'] = []
    for v in tmpbuf['high']:
        ret['high'].append(tmpbuf['high'][v])
    ret['maximum'] = sorted(ret['maximum'], key=lambda k: k['count'], \
        reverse=True)
    ret['high'] = sorted(ret['high'], key=lambda k: k['count'], \
        reverse=True)
    return ret

def compliance_count(compset):
    ret = {}

    ret['maximum'] = {'pass': 0, 'fail': 0}
    ret['high'] = {'pass': 0, 'fail': 0}
    ret['mediumlow'] = {'pass': 0, 'fail': 0}
    for i in compset:
        if i.failvuln.cvss >= 9:
            tag = 'maximum'
        elif i.failvuln.cvss >= 7 and i.failvuln.cvss < 9:
            tag = 'high'
        else:
            tag = 'mediumlow'
        if i.failed:
            ret[tag]['fail'] += 1
        else:
            ret[tag]['pass'] += 1
    return ret

def age_average(vulnset):
    ret = {}
    setbuf = {'maximum': [], 'high': [], 'mediumlow': []}
    if len(vulnset) == 0:
        return None
    for a in vulnset:
        for v in vulnset[a]:
            if v.cvss >= 9:
                avgset = 'maximum'
            elif v.cvss >= 7 and v.cvss < 9:
                avgset = 'high'
            else:
                avgset = 'mediumlow'
            setbuf[avgset].append(v.age_days)
    ret['maximum'] = reduce(lambda x, y: x + y, setbuf['maximum']) \
        / len(setbuf['maximum'])
    ret['high'] = reduce(lambda x, y: x + y, setbuf['high']) \
        / len(setbuf['high'])
    ret['mediumlow'] = reduce(lambda x, y: x + y, setbuf['mediumlow']) \
        / len(setbuf['mediumlow'])
    return ret

def host_impact(vulnset):
    ret = []
    for a in vulnset:
        suma = 0
        cnt = 0
        for v in vulnset[a]:
            suma += v.cvss
            cnt += 1
        ret.append({'assetid': a, 'score': suma, 'count': cnt})
    ret = sorted(ret, key=lambda k: k['score'], reverse=True)
    return ret

def vuln_impact(vulnset):
    ret = []
    vbuf = {}
    for a in vulnset:
        for v in vulnset[a]:
            if v.vid not in vbuf:
                vbuf[v.vid] = {'count': 0, 'score': 0, 'age_set': [],
                    'title': v.title}
            vbuf[v.vid]['count'] += 1
            vbuf[v.vid]['score'] += v.cvss
            vbuf[v.vid]['age_set'].append(v.age_days)
    for i in vbuf:
        v = vbuf[i]
        v['ageavg'] = reduce(lambda x, y: x + y, v['age_set']) \
            / len(v['age_set'])
        del v['age_set']
    for v in vbuf:
        new = vbuf[v]
        new['vid'] = v
        ret.append(new)
    ret = sorted(ret, key=lambda k: k['score'], reverse=True)
    return ret

def node_impact_count(vulnset):
    ret = {'maximum': 0, 'high': 0, 'mediumlow': 0}
    for a in vulnset:
        maxcvss = 0
        for v in vulnset[a]:
            if v.cvss > maxcvss:
                maxcvss = v.cvss
        if maxcvss >= 9:
            cntset = 'maximum'
        elif maxcvss >= 7 and maxcvss < 9:
            cntset = 'high'
        else:
            cntset = 'mediumlow'
        if maxcvss > 0:
            ret[cntset] += 1
    return ret

def find_vid(vid, vbuf):
    for v in vbuf:
        if v.vid == vid:
            return v

def find_resolved(vmd):
    if len(vmd.previous_states) == 0:
        return []

    cur = vmd.current_state
    old = vmd.previous_states[0]

    vbuf = {}
    for a in old:
        for v in old[a]:
            if v.vid not in vbuf:
                vbuf[v.vid] = {'vid': v.vid, 'count': 0, 'resolved': 0,
                    'title': None, 'cvss': 0}
            # See if the issue is found in the current window, if not it's
            # marked as resolved.
            vbuf[v.vid]['title'] = v.title
            vbuf[v.vid]['cvss'] = v.cvss
            if a not in cur:
                vbuf[v.vid]['resolved'] += 1
                continue
            tmpv = find_vid(v.vid, cur[a])
            if tmpv == None:
                vbuf[v.vid]['resolved'] += 1
                continue
            vbuf[v.vid]['count'] += 1
    ret = []
    for v in vbuf:
        new = vbuf[v]
        new['vid'] = v
        ret.append(new)
    return ret

def avg_resolution(vmd):
    def get_total_seconds(td):
        return (td.microseconds + (td.seconds + td.days * 24 * 3600) \
            * 1e6) / 1e6

    candidates = {'maximum': [], 'high': [], 'mediumlow': []}
    for a in vmd.hist:
        for vid in vmd.hist[a]:
            vent = vmd.hist[a][vid]
            iscand = False
            if a not in vmd.current_state:
                iscand = True
            else:
                if find_vid(vid, vmd.current_state[a]) == None:
                    iscand = True
            if not iscand:
                continue
            if vent['vulnerability'].cvss >= 9:
                cntset = 'maximum'
            elif vent['vulnerability'].cvss >= 7 and \
                vent['vulnerability'].cvss < 9:
                cntset = 'high'
            else:
                cntset = 'mediumlow'
            candidates[cntset].append(vent)

    tmpbuf = {}
    ret = {}
    for lbl in candidates.keys():
        tmpbuf[lbl] = []
        for ent in candidates[lbl]:
            delta = ent['last_date'] - ent['first_date']
            tmpbuf[lbl].append(get_total_seconds(delta))
        ret[lbl] = reduce(lambda x, y: x + y, tmpbuf[lbl]) \
            / len(tmpbuf[lbl])
    return ret

def dataset_statestat(vmd):
    debug.printd('summarizing state statistics...')
    vmd.current_statestat['ageavg'] = \
        age_average(vmd.current_state)
    vmd.current_statestat['nodeimpact'] = \
        node_impact_count(vmd.current_state)
    vmd.current_statestat['hostimpact'] = \
        host_impact(vmd.current_state)
    vmd.current_statestat['vulnimpact'] = \
        vuln_impact(vmd.current_state)
    vmd.current_statestat['resolved'] = \
        find_resolved(vmd)
    # XXX Disabled for now.
    #vmd.current_statestat['avgrestime'] = \
    #    avg_resolution(vmd)

    for i in vmd.previous_states:
        newval = {}
        newval['ageavg'] = age_average(i)
        newval['nodeimpact'] = node_impact_count(i)
        newval['hostimpact'] = host_impact(i)
        newval['vulnimpact'] = vuln_impact(i)
        vmd.previous_statestat.append(newval)

def dataset_compstat(vmd):
    debug.printd('summarizing compliance statistics...')
    vmd.current_compstat['passfailcount'] = \
        compliance_count(vmd.current_compliance)
    vmd.current_compstat['impactsum'] = \
        compliance_impactsum(vmd.current_compliance)

    for i in vmd.previous_compliance:
        newval = {}
        newval['passfailcount'] = compliance_count(i)
        newval['impactsum'] = compliance_impactsum(i)
        vmd.previous_compstat.append(newval)

def dataset_compliance(vmd):
    debug.printd('calculating current state compliance...')
    vmd.current_compliance = vmd_compliance(vmd.current_state)
    debug.printd('calculating previous state compliance...')
    for i in vmd.previous_states:
        vmd.previous_compliance.append(vmd_compliance(i))

def dataset_fetch(scanner, gid, window_start, window_end):
    vmd = VMDataSet()

    # Export current state information for the asset group.
    debug.printd('fetching vulnerability data for end of window')
    vmd.current_state = vulns_at_time(scanner, gid, window_end)

    wndsize = window_end - window_start
    for i in range(3):
        wnd_end = window_end - ((i + 1) * wndsize)
        debug.printd('fetching previous window data (%s)' % wnd_end)
        vmd.previous_states.append(vulns_at_time(scanner, gid, wnd_end))

    # Grab historical information. We apply 3 extra windows of the specified
    # size to the query (e.g., if the reporting window is one month we will
    # query back 3 months. This is primarily to gain enough information to
    # identify trends.
    trend_start = window_start - ((window_end - window_start) * 3)
    debug.printd('fetching historical findings from %s to %s' % \
        (trend_start, window_end))

    # XXX Disable historical query for now.
    # This query is expensive and currently takes an extremely long time to
    # complete. It's only used for average resolution time right now so
    # temporarily disabled.
    #vmd.hist = vulns_over_time(scanner, gid, trend_start, window_end)

    dataset_statestat(vmd)

    dataset_compliance(vmd)
    dataset_compstat(vmd)

    return vmd

#
# ASCII output functions
#

txtout = sys.stdout

class DataTable(object):
    def __init__(self, outmode):
        self._outmode = outmode
        self._table = None
        self._csv = None

        if self._outmode == OUTMODE_ASCII:
            self._table = Texttable()
        elif self._outmode == OUTMODE_CSV:
            self._csv = csv.writer(txtout)

    def addrow(self, vals):
        if self._table != None:
            self._table.add_row(vals)
        if self._csv != None:
            self._csv.writerow(vals)

    def tableprint(self):
        if self._table != None:
            report_write(self._table.draw() + '\n')

def report_write(s):
    txtout.write(s)

def ascii_output(vmd):
    report_write('Compliance Summary\n')
    report_write('------------------\n\n')
    ascii_compliance_status(vmd)
    report_write('\n')
    ascii_compliance_trends(vmd)
    report_write('\n')
    # XXX Disable for now.
    #ascii_res(vmd)
    #report_write('\n')
    ascii_outside_compliance(vmd, 'maximum')
    report_write('\n')
    ascii_outside_compliance(vmd, 'high')
    report_write('\n')

    report_write('Current State Summary\n')
    report_write('---------------------\n\n')
    ascii_impact_status(vmd)
    report_write('\n')
    ascii_age_status(vmd)
    report_write('\n')
    ascii_nodes_impact(vmd)
    report_write('\n')
    ascii_issues_resolved(vmd)
    report_write('\n')

    report_write('Trending\n')
    report_write('--------\n')
    ascii_impact_trend(vmd)
    report_write('\n')
    ascii_age_trend(vmd)
    report_write('\n')

    report_write('Host Details\n')
    report_write('------------\n')
    ascii_host_impact(vmd)
    report_write('\n')

    report_write('Vulnerability Details\n')
    report_write('---------------------\n')
    ascii_vuln_impact(vmd)

def ascii_outside_compliance(vmd, label):
    report_write('## Outside Compliance Window (%s)\n' % label)
    t = DataTable(report_outmode)

    t.addrow(['Title', 'Instances', 'Associated Bugs'])
    for i in vmd.current_compstat['impactsum'][label]:
        t.addrow([i['title'], i['count'], 'NA'])

    t.tableprint()

def ascii_vuln_impact(vmd):
    report_write('## Top Issues by Impact\n')

    t = DataTable(report_outmode)

    t.addrow(['Title', 'Instances', 'Cumulative Impact'])
    for i in range(20):
        v = vmd.current_statestat['vulnimpact'][i]
        t.addrow([v['title'], v['count'], '%.2f' % v['score']])

    t.tableprint()

def ascii_host_impact(vmd):
    report_write('## Top Hosts by Impact\n')

    t = DataTable(report_outmode)

    t.addrow(['Hostname', 'Address', 'Vulnerabilities', 'Cumulative Impact'])
    for i in range(20):
        if i >= len(vmd.current_statestat['hostimpact']):
            break
        chost = vmd.current_statestat['hostimpact'][i]
        aptr = vmd.current_state[chost['assetid']]
        if len(aptr) == 0:
            raise Exception('asset entry with no issues')
        hname = aptr[0].hostname
        addr = aptr[0].ipaddr
        t.addrow([hname, addr, chost['count'], chost['score']])
    t.tableprint()

def ascii_issues_resolved(vmd):
    report_write('## Issues Resolved\n')
    d = sorted(vmd.current_statestat['resolved'], \
        key=lambda k: k['resolved'], reverse=True)

    t = DataTable(report_outmode)
    t.addrow(['Title', 'Resolved On', 'Remains On', 'Impact'])
    for i in d:
        if i['resolved'] == 0:
            continue
        if i['cvss'] >= 9:
            label = 'maximum'
        elif i['cvss'] >= 7 and i['cvss'] <= 0:
            label = 'high'
        else:
            label = 'mediumlow'
        t.addrow([i['title'], i['resolved'], i['count'], label])
    t.tableprint()

def ascii_age_trend(vmd):
    report_write('## Average Age by Impact over Time\n')

    t = DataTable(report_outmode)
    t.addrow(['Impact', 'Current - 2 (days)', 'Current - 1 (days)',
        'Current'])
    for i in ('maximum', 'high', 'mediumlow'):
        if vmd.previous_statestat[1]['ageavg'] != None:
            ps1s = vmd.previous_statestat[1]['ageavg'][i]
        else:
            ps1s = 'NA'

        if vmd.previous_statestat[0]['ageavg'] != None:
            ps0s = vmd.previous_statestat[0]['ageavg'][i]
        else:
            ps0s = 'NA'

        cs = vmd.current_statestat['ageavg'][i]

        t.addrow([i, ps1s, ps0s, cs])

    t.tableprint()

def ascii_impact_trend(vmd):
    report_write('## Vulnerabilities by Impact over Time\n')

    t = DataTable(report_outmode)
    t.addrow(['Impact', 'Current - 2', 'Current - 1', 'Current'])
    for i in ('maximum', 'high', 'mediumlow'):
        if len(vmd.previous_compstat) > 1:
            pc1s = vmd.previous_compstat[1]['passfailcount'][i]['pass'] + \
                vmd.previous_compstat[1]['passfailcount'][i]['fail']
        else:
            pc1s = 'NA'

        if len(vmd.previous_compstat) > 0:
            pc0s = vmd.previous_compstat[0]['passfailcount'][i]['pass'] + \
                vmd.previous_compstat[0]['passfailcount'][i]['fail']
        else:
            pc0s = 'NA'

        cs = vmd.current_compstat['passfailcount'][i]['pass'] + \
            vmd.current_compstat['passfailcount'][i]['fail']

        t.addrow([i, pc1s, pc0s, cs])

    t.tableprint()

def ascii_impact_status(vmd):
    report_write('## Vulnerabilities by Impact\n')

    t = DataTable(report_outmode)
    t.addrow(['Impact', 'Count'])
    for i in ('maximum', 'high', 'mediumlow'):
        pfc = vmd.current_compstat['passfailcount']
        cnt = pfc[i]['pass'] + pfc[i]['fail']
        t.addrow([i, cnt])

    t.tableprint()

def ascii_nodes_impact(vmd):
    report_write('## Nodes by Impact\n')

    t = DataTable(report_outmode)
    t.addrow(['Impact', 'Number of Nodes'])
    for i in ('maximum', 'high', 'mediumlow'):
        t.addrow([i, vmd.current_statestat['nodeimpact'][i]])

    t.tableprint()

def ascii_age_status(vmd):
    report_write('## Age by Impact\n')

    t = DataTable(report_outmode)
    t.addrow(['Impact', 'Average Age (days)'])
    for i in ('maximum', 'high', 'mediumlow'):
        t.addrow([i, '%.2f' \
            % vmd.current_statestat['ageavg'][i]])

    t.tableprint()

def ascii_res(vmd):
    report_write('## Current Average Resolution Time\n')

    t = DataTable(report_outmode)
    t.addrow(['Impact', 'Average (Days)'])
    for i in ('maximum', 'high', 'mediumlow'):
        rsec = vmd.current_statestat['avgrestime'][i]
        daystr = '%.2f' % (rsec / 60 / 60 / 24)
        t.addrow([i, daystr])

    t.tableprint()

def ascii_compliance_trends(vmd):
    report_write('## Compliance Trends\n')

    t = DataTable(report_outmode)
    t.addrow(['Impact', 'Current - 2 (In/Out)',
        'Current - 1 (In/Out)', 'Current'])
    for i in ('maximum', 'high', 'mediumlow'):
        if len(vmd.previous_compstat) > 1:
            pc1s = '%d/%d' % \
                (vmd.previous_compstat[1]['passfailcount'][i]['pass'],
                vmd.previous_compstat[1]['passfailcount'][i]['fail'])
        else:
            pc1s = 'NA'

        if len(vmd.previous_compstat) > 0:
            pc0s = '%d/%d' % \
                (vmd.previous_compstat[0]['passfailcount'][i]['pass'],
                vmd.previous_compstat[0]['passfailcount'][i]['fail'])
        else:
            pc0s = 'NA'

        cs = '%d/%d' % (vmd.current_compstat['passfailcount'][i]['pass'],
            vmd.current_compstat['passfailcount'][i]['fail'])

        t.addrow([i, pc1s, pc0s, cs])

    t.tableprint()

def ascii_compliance_status(vmd):
    report_write('## Vulnerability Compliance Status\n')

    t = DataTable(report_outmode)
    dptr = vmd.current_compstat['passfailcount']
    t.addrow(['Impact', 'In Compliance', 'Out of Compliance'])
    for i in ('maximum', 'high', 'mediumlow'):
        t.addrow([i, dptr[i]['pass'], dptr[i]['fail']])
    t.tableprint()
