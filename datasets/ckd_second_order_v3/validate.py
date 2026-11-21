"""Audit the released CSVs independently of the generator. Run in the release directory."""
from collections import Counter, defaultdict
import csv
import hashlib
from itertools import groupby
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
FIELDS = dict(age="age", htn="hypertension", diab="diabetes", cvd="cardiovascular disease",
              bmi="body mass index", sex="sex", edu="education level", smkstat="smoking status",
              inc="household income", pa="physical activity", sodium="sodium intake", rce="race/ethnicity")
CODES = {v: k for k, v in FIELDS.items()}
OPTION = re.compile(r"^[A-E]\. (.+)$", re.M)
FACT = re.compile(r"^.+ \((\w+)\) is (.+)\.$")

def read(path):
    with path.open(encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)

def decode(block):
    head, *lines = block.splitlines()
    facts = dict(FACT.fullmatch(line).groups() for line in lines)
    assert len(facts) == len(lines)
    return head, facts

def verify():
    patients = {r['item_id']: r for r in read(ROOT/'patients.csv')}
    assert len(patients) == 150 and Counter(r['label'] for r in patients.values()) == {'0':75,'1':75}
    assert Counter(r['block_order'] for r in patients.values()) == {'own_first':75,'person_first':75}
    eligible = [r for r in patients.values() if r['direction_contrast_eligible']=='1']
    assert len(eligible) == 64
    assert set(Counter((r['false_direction'], r['block_order']) for r in eligible).values()) == {16}
    seen = set()
    reference = {}
    presentations = {}
    canonical = {}
    check_balance = defaultdict(Counter)
    for supplement in (False, True):
        suffix = '_high_thresholds' if supplement else ''
        path = ROOT / f'ckd_second_order_v3{suffix}.csv'
        visited = set()
        ks = ('1','0.5','0.25') if supplement else ('2','4','9','19')
        questions = {f'{kind}_{k}' for kind in ('cost','threshold','bet') for k in ks}
        if not supplement:
            questions.update(('probability','decision','quintile','likert'))
        count = 0
        for i, records in groupby(read(path), key=lambda r:r['item_id']):
            assert i in patients and i not in visited
            visited.add(i)
            records = list(records)
            patient = patients[i]
            bykey = {(r['setting'],r['case'],r['replicate'],r['question_id'],r['order']):r for r in records}
            expected = set()
            for setting, cases in [('shared',('A','D','D2')),('partial',('B','M','M2','C')),('false',('B','M','M2','C'))]:
                for case in cases:
                    qs = questions | ({'check'} if case=='C' and not supplement else set())
                    for q in qs:
                        for o in (('normal',) if q=='probability' else ('normal','flip')):
                            for rep in (('1','2') if case in ('A','B') else ('1',)):
                                expected.add((setting,case,rep,q,o))
            assert set(bykey)==expected and len(records)==len(expected)
            anchor = 'threshold_1' if supplement else 'probability'
            evidence = {s:decode(bykey[s,c,'1',anchor,'normal']['prompt'].split('\n\n')[0])[1]
                        for s,c in [('shared','A'),('partial','B'),('false','B')]}
            assert reference.setdefault(i,evidence)==evidence
            full, partial, false = (evidence[s] for s in ('shared','partial','false'))
            assert set(full)==set(false)==set(FIELDS) and len(partial)==9 and partial.items()<=full.items()
            altered = {k for k in full if full[k]!=false[k]}
            assert len(altered)==2
            for (s,c,b,q,o), row in bykey.items():
                prompt = row['prompt']
                digest = hashlib.sha256(prompt.encode()).digest()
                assert digest not in seen
                seen.add(digest)
                assert '80.0 years' not in prompt and row['label']==patient['label']
                parts = prompt.split('\n\n')
                evidence_roles = {'A':['own'],'B':['own'],'M':['person'],'D':['person']}.get(c,['own','person'])
                two = len(evidence_roles)==2
                order = patient['block_order'] if two else ''
                assert row['block_order']==order
                if order=='person_first':
                    evidence_roles.reverse()
                assert row['person_name']==(patient['person_name'] if 'person' in evidence_roles else '')
                for role, block in zip(evidence_roles,parts):
                    head, actual = decode(block)
                    if role=='person':
                        assert head.startswith(patient['person_name']+' ') and 'accepts its contents as accurate' in head
                    else:
                        assert head.startswith(('You have','Your record'))
                    target = full if (role=='own' and c=='C') else evidence[s]
                    assert actual==target, (i,s,c,role)
                # Fact order within a case and replicate is fixed across questions
                # and files; all elicitation methods in that trial see identical text.
                prefix = tuple(parts[:len(evidence_roles)])
                assert presentations.setdefault((i,s,c,b),prefix)==prefix
                if (c,q)==('C','probability'):
                    canonical[i,s]=prompt
                question = '\n\n'.join(parts[len(evidence_roles):])
                opts = OPTION.findall(question)
                if q!='probability':
                    assert len(opts)==(5 if q in ('likert','quintile') else 2)
                if o=='flip':
                    normal = bykey[s,c,b,q,'normal']['prompt']
                    assert opts==OPTION.findall(normal)[::-1]
                    assert OPTION.sub('',normal)==OPTION.sub('',prompt)
                if b=='2':
                    assert parts[0]!=bykey[s,c,'1',q,o]['prompt'].split('\n\n')[0]
                changes=set(row['changed_facts'].split('; ')) if row['changed_facts'] else set()
                expected_changes = set()
                if s=='partial':
                    expected_changes={FIELDS[k] for k in full.keys()-partial.keys()}
                if s=='false':
                    expected_changes={f'{FIELDS[k]}: {full[k]} -> {false[k]}' for k in altered}
                assert changes==expected_changes
                assert row['direction']==(patient['false_direction'] if s=='false' else '')
                assert row['direction_assignment']==(patient['direction_assignment'] if s=='false' else '')
                assert (patient['direction_assignment']=='random')==(patient['direction_contrast_eligible']=='1')
                if q=='check':
                    field = row['check_fact']
                    code = CODES[field]
                    assert question.splitlines()[0].endswith(field+'?')
                    answer = opts['AB'.index(row['check_answer'])]
                    if s=='false':
                        relation = 'changed' if code in altered else 'unchanged'
                        assert answer==false[code] and full[code] in opts and len(set(opts))==2
                        group=(s, field, full[code],tuple(opts),o)
                    else:
                        relation = 'shown' if code in partial else 'withheld'
                        assert answer==('Yes' if code in partial else 'No')
                        group=(s,field,tuple(opts),o)
                    assert relation==row['check_relation']
                    check_balance[group][relation]+=1
                else:
                    assert not any(row[k] for k in ('check_fact','check_relation','check_answer'))
            count+=len(records)
        assert visited==patients.keys() and count==(37800 if supplement else 65700)
        print(f'{path.name}: {count:,} rows, {len(visited)} patients')
    assert all(len(v)==2 and len(set(v.values()))==1 for v in check_balance.values())
    audits = {}
    orientations = Counter()
    for row in read(ROOT/'ckd_second_order_v3_role_swap.csv'):
        i,s,c,q,o = (row[k] for k in ('item_id','setting','case','question_id','order'))
        key = (i,s,c,q,o)
        assert key not in audits
        audits[key]=row
        digest = hashlib.sha256(row['prompt'].encode()).digest()
        assert digest not in seen
        seen.add(digest)
        assert row['label']==patients[i]['label'] and row['person_name']==patients[i]['person_name']
        parts = row['prompt'].split('\n\n',2)
        records = [decode(part) for part in parts[:2]]
        own_first = records[0][0].startswith("Your record")
        assert row['block_order']==('own_first' if own_first else 'person_first')
        assert 'accepts its contents as accurate' in records[int(own_first)][0]
        assert records[int(own_first)][0].startswith(row['person_name']+' ')
        assert row['direction_assignment']==(patients[i]['direction_assignment'] if s=='false' else '')
        assert row['direction']==(patients[i]['false_direction'] if s=='false' else '')
        if q=='probability':
            original = canonical[i,s].split('\n\n',2)
            assert c=='C_swap' and o=='normal' and original[-1]==parts[-1]
            for before, after in zip(original[:2],parts[:2]):
                h1,f1=before.split('\n',1)
                h2,f2=after.split('\n',1)
                assert h1!=h2 and f1==f2
            continue
        assert q=='role_check' and c in ('probe','probe_swap')
        assert row['check_fact'] in parts[2].splitlines()[0]
        code = CODES[row['check_fact']]
        person = records[int(own_first)][1]
        other = records[int(not own_first)][1]
        opts = OPTION.findall(parts[2])
        answer = opts['AB'.index(row['check_answer'])]
        if s=='partial':
            assert len(person)==len(other)==9 and len(person.keys()&other.keys())==8
            assert (code in person)!=(code in other)
            assert person.items()<=reference[i]['shared'].items() and other.items()<=reference[i]['shared'].items()
            assert answer==('Yes' if code in person else 'No')
            assert row['check_relation']==('shown' if code in person else 'withheld')
            orientations[c,o,answer]+=1
        else:
            assert answer==person[code]!=other[code] and set(opts)=={person[code],other[code]}
            assert row['check_relation']=='conflicting'
            target = canonical[i,s] if c=='probe' else audits[i,s,'C_swap','probability','normal']['prompt']
            assert parts[:2]==target.split('\n\n',2)[:2]
    for i in patients:
        for s in ('partial','false'):
            assert (i,s,'C_swap','probability','normal') in audits
            for o in ('normal','flip'):
                base,swap=(audits[i,s,c,'role_check',o] for c in ('probe','probe_swap'))
                assert base['check_answer']!=swap['check_answer']
                baseparts,swapparts=(r['prompt'].split('\n\n',2) for r in (base,swap))
                assert baseparts[-1]==swapparts[-1]
                assert all(x.split('\n',1)[1]==y.split('\n',1)[1] and x.splitlines()[0]!=y.splitlines()[0]
                           for x,y in zip(baseparts[:2],swapparts[:2]))
            for c in ('probe','probe_swap'):
                normal,flip=(audits[i,s,c,'role_check',o]['prompt'] for o in ('normal','flip'))
                assert OPTION.sub('',normal)==OPTION.sub('',flip)
                assert OPTION.findall(normal)==OPTION.findall(flip)[::-1]
    assert set(orientations.values())=={75} and len(orientations)==8
    assert len(audits)==1500
    print('Role-swap battery: 1,500 rows, 300 direct comparisons and 300 paired retrieval checks in both option orders.')
    print(f'{len(seen):,} unique prompts. Evidence, keys, flips, checks and allocation passed.')

if __name__=='__main__':
    verify()
