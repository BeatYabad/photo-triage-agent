"""Indexed perceptual candidates, explicitly separate from exact-byte identity."""


class CandidateIndex:
    def __init__(self, distance=10):
        if type(distance) is not int or not 0<=distance<=40:
            raise ValueError('Invalid 256-bit perceptual distance')
        self.distance=distance
        self.roots={}

    def add(self, row):
        code=int(row['phash'],16)
        if len(row['phash'])!=64:raise ValueError('Expected 256-bit pHash')
        library=row['library']
        root=self.roots.get(library)
        matches=[]
        stack=[root] if root else []
        while stack:
            node=stack.pop()
            distance=(node['code']^code).bit_count()
            if distance<=self.distance:
                for other in node['rows']:
                    if (other['sha256'],other['bytes'])!=(row['sha256'],row['bytes']):
                        matches.append({'left':other,'right':row,'distance':distance,
                                        'bits':256,'deletion_authorized':False})
            stack.extend(child for edge,child in node['children'].items()
                         if distance-self.distance<=edge<=distance+self.distance)
        if root is None:
            self.roots[library]={'code':code,'rows':[row],'children':{}}
        else:
            node=root
            while True:
                distance=(node['code']^code).bit_count()
                if distance==0:
                    node['rows'].append(row)
                    break
                if distance not in node['children']:
                    node['children'][distance]={'code':code,'rows':[row],'children':{}}
                    break
                node=node['children'][distance]
        return matches


def candidates(rows, distance=10):
    index=CandidateIndex(distance)
    for row in rows:
        yield from index.add(row)
