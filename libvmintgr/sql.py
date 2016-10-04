# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import sqlite3
import datetime
import pytz
import calendar
import time
import sys

import vuln
import debug

class VMIntDB(object):
    SVER = 1

    def __init__(self, path):
        self._path = path
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row

    def add_version(self):
        c = self._conn.cursor()
        c.execute('''INSERT INTO status VALUES (?)''', (self.SVER,))
        self._conn.commit()
        
    def create(self):
        c = self._conn.cursor()
        c.execute('''PRAGMA foreign_keys = ON''')
        c.execute('''CREATE TABLE IF NOT EXISTS status (version INTEGER)''')
        c.execute('''SELECT MAX(version) FROM status''')
        rows = c.fetchall()

        v = rows[0][0]
        if v == None:
            self.add_version()

        c.execute('''CREATE TABLE IF NOT EXISTS assets
            (id INTEGER PRIMARY KEY, uid TEXT, nxaid INTEGER, ip TEXT,
            hostname TEXT, mac TEXT,
            UNIQUE (uid))''')

        c.execute('''CREATE TABLE IF NOT EXISTS vulns
            (id INTEGER PRIMARY KEY, nxvid INTEGER,
            title TEXT, cvss REAL,
            known_exploits INTEGER, known_malware INTEGER,
            description TEXT, cvss_vector TEXT,
            UNIQUE(nxvid))''')

        c.execute('''CREATE TABLE IF NOT EXISTS assetvulns
            (id INTEGER PRIMARY KEY, aid INTEGER, vid INTEGER,
            detected INTEGER, age REAL, autogroup STRING,
            proof STRING,
            UNIQUE (aid, vid),
            FOREIGN KEY(aid) REFERENCES assets(id),
            FOREIGN KEY(vid) REFERENCES vulns(id))''')

        c.execute('''CREATE TABLE IF NOT EXISTS workflow
            (id INTEGER PRIMARY KEY, vid INTEGER,
            lasthandled INTEGER, contact INTEGER,
            status INTEGER,
            FOREIGN KEY(vid) REFERENCES assetvulns(id))''')

        c.execute('''CREATE TABLE IF NOT EXISTS compliance
            (id INTEGER PRIMARY KEY, aid INTEGER,
            failed INTEGER, link TEXT,
            lastupdated INTEGER, failingvid INTEGER,
            FOREIGN KEY (aid) REFERENCES assets(id))''')

        c.execute('''CREATE TABLE IF NOT EXISTS cves
            (id INTEGER PRIMARY KEY, vid INTEGER, cve TEXT,
            UNIQUE (vid, cve),
            FOREIGN KEY (vid) REFERENCES vulns(id))''')

        c.execute('''CREATE TABLE IF NOT EXISTS rhsas
            (id INTEGER PRIMARY KEY, vid INTEGER, rhsa TEXT,
            UNIQUE (vid, rhsa),
            FOREIGN KEY (vid) REFERENCES vulns(id))''')

    def add_references(self, v, vid):
        c = self._conn.cursor()

        if v.cves != None:
            for cve in v.cves:
                c.execute('''INSERT INTO cves VALUES (NULL,
                    ?, ?)''', (vid, cve))
        if v.rhsa != None:
            for rhsa in v.rhsa:
                c.execute('''INSERT INTO rhsas VALUES (NULL,
                    ?, ?)''',(vid, rhsa))
        self._conn.commit()
            
    def resolve_expired_hosts(self, foundlist):
        c = self._conn.cursor()

        c.execute('''SELECT id, uid FROM assets''')
        rows = c.fetchall()
        for i in rows:
            if i[1] not in foundlist:
                self.resolve_workflow_for_asset(i[0])
    
    def asset_list(self):
        ret = []
        c = self._conn.cursor()
        c.execute('''SELECT id FROM assets''')
        rows = c.fetchall()
        for i in rows:
            ret.append(i[0])
        return ret

    def compliance_update(self, uid, failflag, failvid):
        c = self._conn.cursor()

        failed = 0
        if failflag:
            failed = 1 
        if failvid == None:
            fvid = 0
        else:
            fvid = failvid
        c.execute('''UPDATE compliance SET failed = ?,
            lastupdated = ?, failingvid = ?
            WHERE aid IN (SELECT id FROM assets WHERE uid = ?)''', \
            (failed, int(calendar.timegm(time.gmtime())), fvid, uid))
        self._conn.commit()

    def compliance_values(self, uid):
        c = self._conn.cursor()

        # Return a list to the calculator which is as follows:
        #
        # ((assetvulns:id, cvss, age(days)), ...)
        c = self._conn.cursor()
        c.execute('''SELECT assetvulns.id, vulns.cvss, assetvulns.age
            FROM assetvulns JOIN vulns ON assetvulns.vid = vulns.id
            JOIN assets ON assetvulns.aid = assets.id
            WHERE assets.uid = ?''', (uid,))
        rows = c.fetchall()
        return rows

    def workflow_handled(self, wfid, flag):
        c = self._conn.cursor()
        c.execute('''UPDATE workflow SET status = ?, lasthandled = ?
            WHERE id = ?''', (flag, int(calendar.timegm(time.gmtime())), wfid))
        self._conn.commit()

    def aid_to_host(self, aid):
        c = self._conn.cursor()
        c.execute('''SELECT hostname FROM assets WHERE id = ?''', (aid,))
        rows = c.fetchall()
        if len(rows) == 0:
            return None
        else:
            return rows[0][0]

    def aid_to_autogroup(self, aid):
        c = self._conn.cursor()
        c.execute('''SELECT ip, hostname, mac FROM assets
            WHERE id = ?''', (aid,))
        rows = c.fetchall()
        if len(rows) == 0:
            return None
        ipaddr = rows[0][0]
        hname = rows[0][1]
        mac = rows[0][2]
        agrp = vuln.vuln_auto_finder(ipaddr, mac, hname)
        return agrp.name

    def get_compliance(self, aid):
        c = self._conn.cursor()
        c.execute('''SELECT assets.id, compliance.id AS cid,
            assets.ip, assets.hostname, assets.mac,
            compliance.lastupdated, compliance.failed,
            vulns.nxvid, vulns.title, vulns.cvss,
            assetvulns.age, assetvulns.autogroup
            FROM assets
            JOIN compliance ON assets.id = compliance.aid
            JOIN assetvulns ON (compliance.failingvid = assetvulns.id
            AND assets.id = assetvulns.aid)
            JOIN vulns ON (assetvulns.vid = vulns.id)
            WHERE assets.id = ?''', (aid,))
        rows = c.fetchall()

        if len(rows) == 0:
            return None
        i = rows[0]

        ce = vuln.ComplianceElement()

        ce.compliance_id = i['cid']
        if i['failed'] == 1:
            ce.failed = True
        else:
            ce.failed = False
        ce.lasthandled = i['lastupdated']

        v = vuln.vulnerability()
        v.assetid = aid
        v.ipaddr = i['ip'].encode('ascii', 'ignore')
        v.macaddr = i['mac'].encode('ascii', 'ignore')
        v.hostname = i['hostname'].encode('ascii', 'ignore')
        v.vid = i['nxvid']
        v.age_days = i['age']
        v.title = i['title'].encode('ascii', 'ignore')
        v.cvss = i['cvss']
        v.autogroup = i['autogroup']
        ce.failvuln = v

        return ce

    def get_workflow(self, aid):
        c = self._conn.cursor()
        c.execute('''SELECT assets.id, workflow.id AS wid,
            assets.ip, assets.hostname, vulns.id AS vid,
            assets.mac, vulns.nxvid, vulns.title, vulns.cvss,
            vulns.known_exploits, vulns.known_malware,
            assetvulns.detected, assetvulns.age,
            workflow.lasthandled, workflow.contact, workflow.status,
            assetvulns.autogroup, vulns.description, vulns.cvss_vector,
            assets.nxaid, assetvulns.proof
            FROM assetvulns
            JOIN assets ON assets.id = assetvulns.aid
            JOIN vulns ON vulns.id = assetvulns.vid
            JOIN workflow ON assetvulns.id = workflow.vid
            WHERE assets.id = ?''', (aid,))
        rows = c.fetchall()

        ret = []
        for i in rows:
            wfe = vuln.WorkflowElement()
            
            wfe.lasthandled = i['lasthandled']
            wfe.contact = i['contact']
            wfe.workflow_id = i['wid']
            wfe.status = i['status']
            wfe.assetid_site = i['nxaid']

            v = vuln.vulnerability()
            v.assetid = aid
            v.ipaddr = i['ip'].encode('ascii', 'ignore')
            v.macaddr = i['mac'].encode('ascii', 'ignore')
            v.hostname = i['hostname'].encode('ascii', 'ignore')
            v.vid = i['nxvid']
            v.autogroup = i['autogroup']
            v.proof = i['proof']

            # All that is stored right now is Nexpose vulnerabilities, so
            # create a classification value including that
            v.vid_classified = 'nexpose:%d' % v.vid

            rowvid = i['vid']
            v.discovered_date_unix = i['detected']
            v.title = i['title'].encode('ascii', 'ignore')
            v.description = i['description'].encode('ascii', 'ignore')
            v.cvss = i['cvss']
            v.cvss_vector = i['cvss_vector']
            v.impact_label = vuln.cvss_to_label(v.cvss)
            v.known_malware = False
            v.known_exploits = False
            if i['known_malware'] != 0:
                v.known_malware = True
            if i['known_exploits'] != 0:
                v.known_exploits = True
            v.age_days = i['age']

            # Based on the score of the vulnerability, include the expected
            # patch time (based on initial detection)
            v.patch_in = vuln.cvss_to_patch_in(v.cvss)

            # Supplement the element with associated CVEs
            c.execute('''SELECT cve FROM cves
                WHERE vid = %d''' % rowvid)
            rows2 = c.fetchall()
            v.cves = []
            for j in rows2:
                v.cves.append(j[0].encode('ascii', 'ignore'))

            wfe.vulnerability = v

            ret.append(wfe)
        return ret

    def resolve_workflow_for_asset(self, assetid):
        c = self._conn.cursor()

        debug.printd('resolving all known issues for expired asset id %d' % assetid)
        c.execute('''UPDATE workflow SET status = ?
            WHERE vid IN (SELECT id FROM assetvulns WHERE aid = ?)''',
            (vuln.WorkflowElement.STATUS_RESOLVED, assetid))
        self._conn.commit()

    def add_vuln_master(self, v):
        c = self._conn.cursor()
        exists = False
        ret = None
        mwf = 0
        exf = 0

        if v.known_exploits:
            exf = 1
        if v.known_malware:
            mwf = 1

        try:
            c.execute('''INSERT INTO vulns VALUES (NULL,
                ?, ?, ?, ?, ?, ?, ?)''', (v.vid, v.title,
                v.cvss, exf, mwf, v.description, v.cvss_vector))
        except sqlite3.IntegrityError:
            exists = True
 
        if exists:
            c.execute('''SELECT id FROM vulns WHERE nxvid = ?''', (v.vid,))
            rows = c.fetchall()
            if len(rows) == 0:
                raise Exception('fatal error requesting vulns entry')

            # Update the CVSS score on the vulnerability to handle cases where
            # source signatures are changed after initial publication.
            c.execute('''UPDATE vulns SET cvss = ? WHERE id = ?''', \
                (v.cvss, rows[0][0]))
            self._conn.commit()

            return rows[0][0]

        ret = c.lastrowid
        self.add_references(v, ret)
        self._conn.commit()
        return ret

    def workflow_check_reset(self, vid):
        # This function is called by add_vulnerability to handle a case where
        # a vulnerability reappears on an asset after it has been resolved.
        # We basically check the vid to see if it is resolved/closed, if it
        # is reset it to new.
        c = self._conn.cursor()

        c.execute('''SELECT status FROM workflow
            WHERE vid = ?''', (vid,))
        rows = c.fetchall()
        if len(rows) == 0:
            return
        sts = rows[0][0]
        if sts == vuln.WorkflowElement.STATUS_RESOLVED or \
            sts == vuln.WorkflowElement.STATUS_CLOSED:
            c.execute('''UPDATE workflow SET status = 0
                WHERE vid = ?''', (vid,))
            debug.printd('reset status on vid %d' % vid)

    def add_vulnerability(self, v, dbassetid, vauto):
        c = self._conn.cursor()

        c.execute('''SELECT assetvulns.id FROM assetvulns
            JOIN vulns on assetvulns.vid = vulns.id
            WHERE vulns.nxvid = ? AND assetvulns.aid = ?''', \
            (v.vid, dbassetid))
        rows = c.fetchall()
        if len(rows) == 0:
            # This is a new issue for this asset
            vulnrow = self.add_vuln_master(v)
            c.execute('''INSERT INTO assetvulns VALUES (NULL, ?,
                ?, ?, ?, ?, ?)''', (dbassetid, vulnrow,
                v.discovered_date_unix, v.age_days, vauto.name,
                v.proof))
            entrow = c.lastrowid
            c.execute('''INSERT INTO workflow VALUES (NULL, ?,
                0, ?, 0)''', (entrow, int(calendar.timegm(time.gmtime()))))
        else:
            c.execute('''UPDATE assetvulns SET detected = ?,
                age = ? WHERE
                id = ?''', (v.discovered_date_unix, v.age_days, rows[0][0]))
            self.workflow_check_reset(rows[0][0])
            # Update the proof associated with this issue as reported by
            # Nexpose, in addition to the assets current autogroup
            c.execute('''UPDATE assetvulns SET proof = ?,
                autogroup = ?
                WHERE assetvulns.id = ?''', (v.proof, vauto.name, rows[0][0]))
        self._conn.commit()

    def resolve_vulnerability(self, vidlist, dbassetid):
        c = self._conn.cursor()

        c.execute('''SELECT vulns.nxvid, assetvulns.id FROM vulns
            JOIN assetvulns ON vulns.id = assetvulns.vid WHERE
            assetvulns.aid = ?''', (dbassetid,))
        rows = c.fetchall()

        for i in rows:
            if i[0] in vidlist:
                continue
            debug.printd('marking vulnerability %d as resolved' % i[1])
            # We previously knew about the vulnerability on the device
            # and it's not there anymore, mark it as resolved
            c.execute('''UPDATE workflow SET status = ?
                WHERE vid = ?''', (vuln.WorkflowElement.STATUS_RESOLVED,
                i[1]))

    def asset_duplicate_resolver(self, uid):
        # Given a UID, try to resolve duplicates in the database, returning a
        # list of asset ids that match the supplied uid
        uidel = uid.split('|')
        uidel[1] = '%'
        searchuid = '|'.join(uidel)
        c = self._conn.cursor()
        c.execute('''SELECT id FROM assets WHERE uid LIKE ?''', (searchuid,))
        rows = c.fetchall()
        return [x[0] for x in rows]

    def asset_search_and_update(self, uid, aid, address, mac, hostname):
        # See if we had any previous instance of this MAC address and
        # hostname, if so update the asset with new value
        if mac == '':
            return
        c = self._conn.cursor()
        c.execute('''SELECT id, uid FROM assets WHERE mac = ?
            AND hostname = ?''', (mac, hostname))
        rows = c.fetchall()
        if len(rows) == 0:
            return
        if rows[0][1] == uid:
            return
        debug.printd('updating information for asset %s' % uid)
        debug.printd('was: %s now: %s' % (rows[0][1], uid))
        c.execute('''UPDATE assets SET uid = ?, ip = ?,
            hostname = ?, nxaid = ? WHERE id = ?''',
            (uid, address, hostname, aid, rows[0][0]))

    def add_asset(self, uid, aid, address, mac, hostname):
        c = self._conn.cursor()
        c.execute('''SELECT id FROM assets WHERE uid = ?''', (uid,))
        rows = c.fetchall()
        if len(rows) == 0:
            # Before we add the asset, make sure this isn't a duplicate being
            # reported by the scanner
            if len(self.asset_duplicate_resolver(uid)) != 0:
                debug.printd('aid %d looks like a duplicate, ignoring' % aid)
                return None
            c.execute('''INSERT INTO assets VALUES (NULL, ?, ?,
                ?, ?, ?)''', (uid, aid, address, hostname, mac))
            ret = c.lastrowid
            # We also want a compliance tracking item for each asset
            c.execute('''INSERT INTO compliance VALUES (NULL, ?, 0,
                NULL, 0, 0)''', (ret,))
            self._conn.commit()
            return ret
        else:
            return rows[0][0]

def db_init(path):
    ret = VMIntDB(path)
    return ret
