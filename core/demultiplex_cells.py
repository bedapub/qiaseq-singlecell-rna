import sys
import gzip
import io
import os
import editdistance
import collections
from extract_multiplex_region import open_by_magic

"""
Demultiplex the R1 fastq file into individual cells based
upon the cell index, incorporate the barcode tag to the ReadID
for downstream anaylsis.
"""

## To do :
## 1. Write a wrapper/workflow to use create_cell_fastqs() (Preferably luigi)
## 2. Manage injestion of parameters
## 3. Improve runtime, currently takes a ~64 secs to multiplex and write fastqs
## for a readset of size 2 million reads
## 4. Write R2 fastq reads as well

def iterate_fastq(fastq):
    '''
    Read a fastq file and return the 4 lines as a list
    '''

    with open_by_magic(fastq) as IN:
        while True:
            line = IN.readline()
            if not line:
                break
            yield [line.strip('\n'),IN.readline().strip('\n'),
                   IN.readline().strip('\n'),
                   IN.readline().strip('\n')]

def read_multiplex_file(multiplex_file):
    '''
    Read the mutliplex file created using extract_multiplex_region.py

    :param str multiplex_file: path to the multiplex region file
    :yield: list of (read2_id,cell_index,mt)
    '''

    with open(multiplex_file,'r') as IN:
        IN.readline()
        for line in IN:
            yield line.strip('\n').split('\t')

def match_cell_index(cell_indices,cell_index,edit_dist):
    '''
    Edit distance match on the cell index

    :param dict {cell_index:cellnumber}
    :param str cell_index
    :param int edit_dist
    :return: Whether the cell index matches an valid list of indices
    :rtype: Bool
    '''

    match = []
    ## To reduce time complexity first check if the cell index is present
    ## in the dictionary, this is an O(1) operation
    if cell_index in cell_indices:
        return True
    else: ## Need to traverse the list of indices and fuzzy match
        if edit_dist > 0:
            for index in cell_indices:
                if edit_dist <= editdistance.eval(index,cell_index):
                    match.append(index)
                    ## Not breaking here because we want to make sure
                    ## that the cell_index has a unique match to only
                    ## 1 of the set of 96/384 cell indices
            if len(match) == 1:
                return True
            else: ## To do: Book keeping when cell index
                  ## matches to 2 or more indices in the list
                return False
        else:
            return False

def create_cell_index_db(multiplex_file):
    '''
    Create a db to store read_id -> cell_index, mt
    Use this for out of memory situations , use the
    shelve module in python.
    '''

def create_read_id_hash(multiplex_file):
    '''
    Creates a dict read_id -> [cell_index,mt]
    :param str multiplex_file: a tsv <read_id> <cell_index> <mt>
    :return: a dictionary of {read_id:[cell_index,mt]}
    :rtype: dict
    '''
    d = {}
    for read_id,cell_index,mt in read_multiplex_file(multiplex_file):
        key = read_id.split()[0]
        d[key] = [cell_index,mt]
    return d

def read_cell_index_file(cell_index_file):
    '''
    Read the file containing the cell indices and
    return it as a dict

    :param str cell_index_file: the cell index file
    :return: list containing the cell indices
    :rtype: dict
    :raises: Exception for duplicate cell index
    '''
    d = collections.OrderedDict()
    i=1
    with open(cell_index_file,'r') as IN:
        for line in IN:
            key = line.strip('\n')
            if key in d:
                raise Exception('Duplicate cell index encountered !')
            d[key] = i
            i+=1
    return d

def write_metrics(metric_file,**metrics):
    '''
    '''
    with open(metric_file,'w') as OUT:
        for key,val in metrics.items():
            OUT.write('{metric}: {value}\n'.format(metric=key,value=val))

def write_fastq(read_info,fastq_loc):
    '''
    Write a fastq file

    :param str read_info: a list whose elements are the 4 lines of a fastq
    :param fastq_loc: full path to the fastq file to write to
    :return: nothing
    '''

    with open(fastq_loc,'a+') as OUT:
        out = '\n'.join(read_info)
        OUT.write(out)

def create_cell_fastqs(base_dir,metric_file,cell_index_file,cell_multiplex_file,read_file1,
                       read_file2):
    '''
    Demultiplex and create individual cell fastqs

    :param str base_dir: base directory to create subfolders and files
    :param str metric_file: name of the metric file to write to
    :param str cell_index_file: file containing the valid cell indices
    :param str cell_multiplex_file: tsv file <read2_id> <cell_index> <mt>
    :param str read_file1: read1 fastq file
    :param str read_file2: read2 fastq file
    :return: nothing
    '''

    i=0
    j=0
    k=0
    cell_indices = read_cell_index_file(cell_index_file)
    read_id_hash = create_read_id_hash(cell_multiplex_file)
    ## Create cell specific dirs
    map(lambda x:os.makedirs(os.path.join(base_dir,x[1]+'_cell_'+str(x[0]+1))),
        enumerate(cell_indices))
    for read_info in iterate_fastq(read_file1):
        read_id,seq,p,qual = read_info
        key = read_id.split()[0]
        if key in read_id_hash:
            cell_index = read_id_hash[key][0]
            ret = match_cell_index(cell_indices,cell_index,1)
            if ret == True:
                i+=1
                cell_num = cell_indices[cell_index]
                fastq_loc = os.path.join(base_dir,cell_index+'_cell_'+
                                         str(cell_num)+
                                         '/cell_'+str(cell_num)+'_R1.fastq')
                write_fastq(read_info,fastq_loc)
            else:
                k+=1
        j+=1
    metric_dict = {
        'num_reads_matched':i,
        'num_reads_not_made_to_downsampling':k,
        'num_reads':j,
        'perc_reads_matched':(float(i)/j)*100
    }
    write_metrics(os.path.join(base_dir,metric_file),**metric_dict)

create_cell_fastqs(sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5],sys.argv[6])
