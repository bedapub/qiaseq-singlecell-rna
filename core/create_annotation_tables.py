import sqlite3
import os
from intervaltree import IntervalTree
from collections import defaultdict
from extract_multiplex_region import open_by_magic



def convert_strand(strand):
    ''' Convert strand to QIAGEN format
    :param str strand: the strand , either + or -
    :return '1' or '-1'
    :rtype str
    '''
    if strand == "+":
        return '1'
    elif strand == '-':
        return '-1'
    else:
        raise Exception("Invalid Strand information !")

def create_gene_hash(annotation_gtf,ercc_bed):
    ''' Create a Hash table with annotation information for Genes

    :param str: annotation_gtf: path to the genic annotation gtf file 
    :param str: ercc_bed: path to the bed file with ERCC information

    :return A dictionary with annotation information
    :rtype dict
    '''
    gene_info = defaultdict(list)
    ## Parse the gencode annotation file and store in the dictionary
    with open_by_magic(annotation_gtf) as IN:
        for line in IN:
            if line[0] == "#":
                continue
            contents = line.strip('\n').split('\t')
            if contents[2] == 'gene':
                chrom = contents[0]
                start = contents[3]
                end = contents[4]
                strand = convert_strand(contents[6])                    
                info = contents[-1]
                gene = info.split(';')[3].split()[1].strip('\"')
                gene_type = info.split(';')[1].split()[1].strip('\"')
                cols = [chrom,start,end,strand,gene,gene_type]
                gene_info[gene] = cols
                
    with open(ercc_bed,'r') as IN:
        for line in IN:
            chrom,start,end,seq,strand,ercc = line.strip("\n").split("\t")
            cols = [chrom,start,end,strand,chrom,ercc]
            gene_info[chrom] = cols

    return gene_info

def create_gene_tree(annotation_gtf,ercc_bed,merge_coordinates=False):
    '''
    :param str annotation_gtf : a gtf file for identifying genic regions
    :param str ercc_bed: a bed file for storing information about ERCC regions

    :return An interval tree with annotation gene and ercc annotation information
    :rtype object : IntervalTree data structure
    '''
    gene_tree = defaultdict(lambda:defaultdict(IntervalTree))
    genes = defaultdict(list)
    valid_chromosomes = ["chr"+str(i) for i in range(0,23)]
    valid_chromosomes.extend(["chrX","chrY","chrM"])
    with open_by_magic(annotation_gtf) as IN:
        for line in IN:
            if line[0]=='#':
                continue
            contents = line.strip('\n').split('\t')
            if contents[2] == 'gene':
                chrom = contents[0]
                if chrom not in valid_chromosomes: ## Skip contigs
                    continue
                start = int(contents[3])
                end = int(contents[4])
                strand = convert_strand(contents[6])
                info = contents[-1]
                gene = info.split(';')[3].split()[1].strip('\"')
                ensembl_id = info.split(';')[0].split()[1].strip('\"')
                gene_type = info.split(';')[1].split()[1].strip('\"')
                

                if gene == None or ensembl_id == None:
                    raise Exception(
                        "Failed Parsing annotation file :{annotation}".format(
                            annotation=annotation_gtf))

                if merge_coordinates: ## Create a coordinate set which is merged to include the largest interval possible
                    if gene in genes: ## Gene has been seen before
                        if genes[gene][2] != chrom: ## Different chromosome
                            genes[gene] = (start,end,chrom,strand,gene,ensembl_id)
                        else:
                            if start <= genes[gene][0]: ## Gene has unique start bases to add
                                if end >= genes[gene][1]: ## Gene has unique end bases to add
                                    genes[gene] = (start,end,chrom,strand,gene,ensembl_id)
                                else:
                                    if end < genes[gene][0]: ## Check if this interval is dijoint from the previous one
                                        genes[gene].append((start,end,chrom,strand,gene,ensembl_id)) ## Add a new interval
                                    else:
                                        genes[gene] = (start,genes[gene][1],chrom,strand,gene,ensembl_id) ## Merge intervals

                            else: ## Start is already spanned , check end
                                if end >= genes[gene][1]: ## Update end base position
                                    if start > genes[gene][1]: ## Start is greater than previously encountered gene's end
                                        genes[gene].append((start,end,chrom,strand,gene,ensembl_id)) ## Add a new interval
                                    else:
                                        genes[gene] = (genes[gene][0],end,chrom,strand,gene,ensembl_id) ## Merge intervals
                                else: ## No need to update anything
                                    continue
                    else:
                        genes[gene] = (start,end,chrom,strand,gene,ensembl_id)

                else: ## Store all intervals without merging
                    genes[gene].append((start,end,chrom,strand,gene,ensembl_id))

    ## ERCC info
    with open(ercc_bed,'r') as IN:
        for line in IN:
            chrom,start,end,seq,strand,ercc = line.strip("\n").split("\t")
            strand = convert_strand(strand)
            if chrom in genes:
                raise Exception("Duplicate ERCC names !")
            genes[chrom].append((int(start),int(end),chrom,strand,chrom,ercc))
    
    ## Build a gene tree to store gene info
    for gene in genes:
        for info in genes[gene]:
            start,end,chrom,strand,gene,ensembl_id = info
            assert strand in ['-1','1'],"Incorrect strand !"
            if strand == "1":
                five_prime = start
                three_prime = end
            else:
                five_prime = end
                three_prime = start
            new_info = (ensembl_id,gene,strand,chrom,five_prime,three_prime)
            gene_tree[chrom][strand].addi(start,end+1,new_info)

    print "Interval tree created with {ngenes}".format(ngenes=len(genes))
    return gene_tree
