import os
import subprocess
import glob
import gzip
import sys
import luigi
import logging
import sqlite3
import ConfigParser
import datetime
## Modules from this project
sys.path.append(os.path.join(os.path.dirname(
    os.path.realpath(__file__)),'core'))
from extract_multiplex_region import extract_region
from demultiplex_cells import create_cell_fastqs
from align_transcriptome import star_alignment,star_load_index,star_remove_index,annotate_bam_umi,run_cmd
from count_mt import count_umis,count_umis_wts
from merge_mt_files import merge_count_files,merge_metric_files
from combine_sample_results import combine_count_files,combine_cell_metrics,combine_sample_metrics,clean_for_clustering,check_metric_counts
from create_excel_sheet import write_excel_workbook
from create_annotation_tables import create_gene_tree,create_gene_hash

## Some globals to cache across tasks
GENE_TREE = None ## IntervalTree datastructure for use in WTS
GENE_HASH = None ## Annotations for genes , for use in Targeted case
## Set up logging
logger = logging.getLogger("pipeline")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler(sys.stdout)
logger.addHandler(ch)

def is_gzip_empty(gzipfile):
    ''' Helper function to check if a gzip file is empty
    :param str gzipfile: the .gz file
    :returns Whether the file is empty or not
    :rtype bool
    '''
    with gzip.open(gzipfile,'r') as IN:
        i=0
        for line in IN:
            if i == 5:
                break
            i+=1
    return i == 0

class config(luigi.Config):
    ''' Initialize values from configuration file
    '''
    star = luigi.Parameter(description="Path to the STAR executable")
    star_params = luigi.Parameter(description="Params for STAR")
    star_load_params = luigi.Parameter(description="Params for STAR to load a genome file")
    genome_dir = luigi.Parameter(description="The path to the star index dir")
    seqtype = luigi.Parameter(description="Whether this is a targetted or wts experiment")
    primer_file = luigi.Parameter(description="The primer file,if wts this is not applicable")
    annotation_gtf = luigi.Parameter(description="Gencode annotation file")
    ercc_bed = luigi.Parameter(description="ERCC bed file with coordinate information")
    is_low_input = luigi.Parameter(description="Whether the sequencing protocol was for a low input application")
    catalog_number = luigi.Parameter(description="The catalog number for this primer pool")
    species = luigi.Parameter(description="The species name")
    
class MyExtTask(luigi.ExternalTask):
    ''' Checks whether the file specified exists on disk
    '''
    file_loc = luigi.Parameter()
    def output(self):
        return luigi.LocalTarget(self.file_loc)

class ExtractMultiplexRegion(luigi.Task):
    ''' Task for extracting the <cell_index><mt> region
    from R2 reads
    '''
    ## The parameters for this task
    R1_fastq = luigi.Parameter()
    R2_fastq = luigi.Parameter()
    output_dir = luigi.Parameter()
    sample_name = luigi.Parameter()
    cell_index_file = luigi.Parameter()
    vector_sequence = luigi.Parameter()
    isolator = luigi.Parameter()
    cell_index_len = luigi.IntParameter()
    mt_len = luigi.IntParameter()
    num_cores = luigi.IntParameter()
    num_errors = luigi.IntParameter()
    instrument = luigi.Parameter()

    def __init__(self,*args,**kwargs):
        ''' The constructor
        '''
        super(ExtractMultiplexRegion,self).__init__(*args,**kwargs)
        ## Set up folder structure
        self.sample_dir = os.path.join(self.output_dir,self.sample_name)
        if not os.path.exists(self.sample_dir):
            os.makedirs(self.sample_dir)
        ## Create a directory for storing verification files for task completion
        self.target_dir = os.path.join(self.sample_dir,'targets')
        if not os.path.exists(self.target_dir):
            os.makedirs(self.target_dir)
        ## Create a directory for storing log files
        self.logdir = os.path.join(self.sample_dir,'logs')
        if not os.path.exists(self.logdir):
            os.makedirs(self.logdir)
        ## The verification file for this task
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.verification.txt')
        self.multiplex_file = os.path.join(self.sample_dir,
                                           '%s_multiplex_region.txt'%self.sample_name)
    def requires(self):
        ''' Dependencies for this task
        R2 fastq file must be present
        '''
        return MyExtTask(self.R2_fastq)

    def run(self):
        ''' Run the function to extract the multiplex region,
        i.e. the <cell_index><MT> sequence
        The resultant file is a tsv <read_id> <cell_index> <MT>
        '''
        logger.info("Started Task: {x}-{y} {z}".format(x='ExtractMultiplexRegion',y=self.sample_name,z=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        extract_region(self.vector_sequence,
                       self.num_errors,self.cell_index_len,
                       self.mt_len,self.isolator,self.R2_fastq,
                       self.multiplex_file,self.num_cores,
                       self.instrument)
        ## Create the verification file
        with open(self.verification_file,'w') as OUT:
            print >> OUT,"verification"
        logger.info("Finished Task: {x}-{y} {z}".format(x='DeMultiplexer',y=self.sample_name,z=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
 
    def output(self):
        ''' Check for the existence of the verification file
        '''
        return luigi.LocalTarget(self.verification_file)

class DeMultiplexer(luigi.Task):
    ''' Task for demultiplexing a fastq into individual cells
    '''
    ## The parameters for this task
    R1_fastq = luigi.Parameter()
    R2_fastq = luigi.Parameter()
    output_dir = luigi.Parameter()
    sample_name = luigi.Parameter()
    cell_index_file = luigi.Parameter()
    vector_sequence = luigi.Parameter()
    isolator = luigi.Parameter()
    cell_index_len = luigi.IntParameter()
    mt_len = luigi.IntParameter()
    num_cores = luigi.IntParameter()
    num_errors = luigi.IntParameter()
    instrument = luigi.Parameter()

    def __init__(self,*args,**kwargs):
        ''' Class constructor
        '''
        super(DeMultiplexer,self).__init__(*args,**kwargs)
        self.sample_dir = os.path.join(self.output_dir,self.sample_name)
        self.temp_metric_file = os.path.join(self.sample_dir,
                                       '%s_read_stats.temp.txt'%self.sample_name)
        self.target_dir = os.path.join(self.sample_dir,'targets')
        ## The verification file for this task
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.verification.txt')
        self.multiplex_file = os.path.join(self.sample_dir,
                                           '%s_multiplex_region.txt'%self.sample_name)

    def requires(self):
        ''' We need the ExtractMultiplexRegion task to be finished
        '''
        return self.clone(ExtractMultiplexRegion)

    def run(self):
        ''' Work entails demultiplexing of Fastqs
        '''
        logger.info("Started Task: {x}-{y} {z}".format(x='DeMultiplexer',y=self.sample_name,z=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        if config().seqtype.upper() == 'WTS':
            create_cell_fastqs(self.sample_dir,self.temp_metric_file,
                               self.cell_index_file,self.multiplex_file,
                               self.R1_fastq,True)
        else:
            create_cell_fastqs(self.sample_dir,self.temp_metric_file,
                               self.cell_index_file,
                               self.multiplex_file,self.R1_fastq)
        ## Create the verification file
        with open(self.verification_file,'w') as OUT:
            print >> OUT,"verification"
        logger.info("Finished Task: {x}-{y} {z}".format(x='DeMultiplexer',y=self.sample_name,z=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

    def output(self):
        ''' Verify the output from this task
        '''
        return luigi.LocalTarget(self.verification_file)

class LoadGenomeIndex(luigi.Task):
    ''' Task for loading genome index for STAR
    '''
    ## Define some parameters
    output_dir = luigi.Parameter(significant=False)

    def __init__(self,*args,**kwargs):
        ''' Class constructor
        '''
        super(LoadGenomeIndex,self).__init__(*args,**kwargs) 
        self.target_dir = os.path.join(self.output_dir,'targets')
        ## The verification file for this task
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.verification.txt')

    def requires(self):
        ''' Dependency for this task is the existence of the genome dir
        '''
        return MyExtTask(config().genome_dir)

    def run(self):
        ''' Work entails loading the genome index
        '''
        logger.info("Started Task: {x} {y}".format(x='LoadGenomeIndex',y=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        star_load_index(config().star,config().genome_dir,config().star_load_params)
        ## Create the verification file
        with open(self.verification_file,'w') as OUT:
            print >> OUT,"verification"
        logger.info("Finished Task: {x} {y}".format(x='LoadGenomeIndex',y=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
 
    def output(self):
        ''' Output from this task is the verification file
        '''
        return luigi.LocalTarget(self.verification_file)

class Alignment(luigi.Task):
    ''' Task for running STAR for alignment
    '''
    ## Define some parameters
    R1_fastq = luigi.Parameter()
    R2_fastq = luigi.Parameter()
    output_dir = luigi.Parameter()
    sample_name = luigi.Parameter()
    cell_index_file = luigi.Parameter()
    vector_sequence = luigi.Parameter()
    isolator = luigi.Parameter()
    cell_index_len = luigi.IntParameter()
    mt_len = luigi.IntParameter()
    num_cores = luigi.IntParameter()
    num_errors = luigi.IntParameter()
    instrument = luigi.Parameter()

    cell_fastq = luigi.Parameter()
    cell_num = luigi.IntParameter()
    cell_index = luigi.Parameter()


    def __init__(self,*args,**kwargs):
        ''' Class constructor
        '''
        super(Alignment,self).__init__(*args,**kwargs)
        self.sample_dir = os.path.join(self.output_dir,self.sample_name)
        self.multiplex_file = os.path.join(self.sample_dir,
                                           '%s_multiplex_region.txt'%self.sample_name)

        self.cell_dir = os.path.join(self.sample_dir,'Cell%i_%s'%(self.cell_num,
                                                                  self.cell_index))
        self.bam = os.path.join(self.cell_dir,'Aligned.sortedByCoord.out.bam')
        self.target_dir = os.path.join(self.sample_dir,'targets')
        ## The verification file for this task
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.'+str(self.cell_num)+
                                              '.verification.txt')
    def requires(self):
        ''' Task requires loading of GenomeIndex and Demultiplexing of Fastqs
        '''
        yield LoadGenomeIndex(output_dir=self.output_dir)
        yield self.clone(DeMultiplexer)

    def run(self):
        ''' Work is to run STAR alignment
        '''
        logger.info("Started Task: {x}-{y}-{z} {v}".format(x='STAR Alignment',y=self.sample_name,z=self.cell_num,v=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        if not is_gzip_empty(self.cell_fastq): ## Make sure the file is not empty
            ## Do the alignment
            star_alignment(config().star,config().genome_dir,
                           os.path.join(self.cell_dir,''),config().star_params,
                           self.cell_fastq)
        ## Create the verification file
        with open(self.verification_file,'w') as OUT:
            print >> OUT,"verification"
        logger.info("Finished Task: {x}-{y}-{z} {v}".format(x='STAR Alignment',y=self.sample_name,z=self.cell_num,v=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

    def output(self):
        ''' Output from this task for verification
        '''
        return luigi.LocalTarget(self.verification_file)

class CountMT(luigi.Task):
    ''' Task for counting MTs, presumably this is the final step
    which gives us a Primer/Gene x Cell count matrix file
    Will likely have some wrapper task to enapsulate this to parallelize
    by Cells
    '''
    ## Parameters
    R1_fastq = luigi.Parameter()
    R2_fastq = luigi.Parameter()
    output_dir = luigi.Parameter()
    sample_name = luigi.Parameter()
    cell_index_file = luigi.Parameter()
    vector_sequence = luigi.Parameter()
    isolator = luigi.Parameter()
    cell_index_len = luigi.IntParameter()
    mt_len = luigi.IntParameter()
    num_cores = luigi.IntParameter()
    num_errors = luigi.IntParameter()
    instrument = luigi.Parameter()

    cell_fastq = luigi.Parameter()
    cell_num = luigi.IntParameter()
    cell_index = luigi.Parameter()

    def __init__(self,*args,**kwargs):
        ''' Class constructor
        '''
        super(CountMT,self).__init__(*args,**kwargs)
        self.sample_dir = os.path.join(self.output_dir,self.sample_name)
        self.cell_dir = os.path.join(self.sample_dir,'Cell%i_%s'%(self.cell_num,self.cell_index))
        self.bam = os.path.join(self.cell_dir,'Aligned.sortedByCoord.out.bam')
        self.outfile = os.path.join(self.cell_dir,'umi_count.txt')
        self.outfile_primer = os.path.join(self.cell_dir,'umi_count.primers.txt')
        self.metricsfile = os.path.join(self.cell_dir,'read_stats.txt')
        self.target_dir = os.path.join(self.sample_dir,'targets')
        self.logdir = os.path.join(self.sample_dir,'logs')
        ## The verification file for this task
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.'+str(self.cell_num)+
                                              '.verification.txt')
        self.logfile = os.path.join(self.logdir,
                                    self.__class__.__name__ +
                                    self.sample_name +
                                    '.'+str(self.cell_num)+'.log.txt')

    def requires(self):
        ''' Requirement is the completion of the Alignment task
        '''
        return self.clone(Alignment)

    def run(self):
        ''' Work to be done is counting of UMIs
        '''
        logger.info("Started Task: {x}-{y}-{z} {v}".format(x='UMI Counting',y=self.sample_name,z=self.cell_num,v=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        if not is_gzip_empty(self.cell_fastq): ## Make sure the file is not empty
            if config().seqtype.upper() == 'WTS':
                count_umis_wts(GENE_TREE,self.bam,self.outfile,
                               self.metricsfile,self.logfile)
            else:
                count_umis(GENE_HASH,config().primer_file,self.bam,
                           self.outfile_primer,self.outfile,
                           self.metricsfile,self.logfile,self.num_cores)

        with open(self.verification_file,'w') as OUT:
            print >> OUT,"verification"
        logger.info("Finished Task: {x}-{y}-{z} {v}".format(x='UMI Counting',y=self.sample_name,z=self.cell_num,v=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

    def output(self):
        ''' The output from this task
        '''
        return luigi.LocalTarget(self.verification_file)

class JoinCountFiles(luigi.Task):
    ''' Task for joining MT count files
    '''
    # Parameters
    R1_fastq = luigi.Parameter()
    R2_fastq = luigi.Parameter()
    output_dir = luigi.Parameter()
    sample_name = luigi.Parameter()
    cell_index_file = luigi.Parameter()
    vector_sequence = luigi.Parameter()
    isolator = luigi.Parameter()
    mt_len = luigi.IntParameter()
    num_cores = luigi.IntParameter()
    num_errors = luigi.IntParameter()
    instrument = luigi.Parameter()

    def __init__(self,*args,**kwargs):
        ''' Class constructor
        '''
        super(JoinCountFiles,self).__init__(*args,**kwargs)
        self.sample_dir = os.path.join(self.output_dir,self.sample_name)
        self.count_file = os.path.join(self.sample_dir,
                                       self.sample_name+'.umi.counts.txt')
        self.count_file_primers = os.path.join(self.sample_dir,
                                       self.sample_name+'.umi.counts.primers.txt')
        
        self.temp_metric_file = os.path.join(self.sample_dir,
                                       '%s_read_stats.temp.txt'%self.sample_name)
        self.metric_file = os.path.join(self.sample_dir,
                                       '%s_read_stats.txt'%self.sample_name)
        self.metric_file_cell = os.path.join(self.sample_dir,
                                             '%s_cell_stats.txt'%self.sample_name)
        ## The verification file for this task
        self.target_dir = os.path.join(self.sample_dir,'targets')
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.verification.txt')
        self.cell_indices = []
        i=0
        with open(self.cell_index_file,'r') as IN:
            for cell_index in IN:
                if i==0:
                    self.cell_index_len = len(cell_index.strip('\n'))
                    i+=1                                          
                self.cell_indices.append(cell_index.strip('\n'))

    
    def requires(self):
        ''' Dependncies are the completion of the individual MT counting tasks
        for each cell
        '''
        ## Schedule the dependencies first
        dependencies = []
        for i,cell_index in enumerate(self.cell_indices):
            cell_num = i+1
            cell_dir = os.path.join(self.sample_dir,'Cell%i_%s'%(
                cell_num,cell_index))
            cell_fastq = os.path.join(cell_dir,'cell_'+str(cell_num)+
                                      '_R1.fastq.gz')
            dependencies.append(CountMT(R1_fastq=self.R1_fastq,
                                        R2_fastq=self.R2_fastq,
                                        output_dir=self.output_dir,
                                        sample_name=self.sample_name,
                                        cell_index_file=self.cell_index_file,
                                        vector_sequence=self.vector_sequence,
                                        isolator=self.isolator,
                                        cell_index_len=self.cell_index_len,
                                        mt_len=self.mt_len,
                                        num_cores=self.num_cores,
                                        num_errors=self.num_errors,
                                        instrument=self.instrument,
                                        cell_fastq=cell_fastq,
                                        cell_num=cell_num,
                                        cell_index=cell_index))
        yield dependencies        

    def run(self):
        ''' Work to be done is merging individual cell files for a given sample
        '''
        logger.info("Started Task: {x}-{y} {z}".format(x='JoinCountFiles',y=self.sample_name,z=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        ## Merge gene level count files first
        files_to_merge = glob.glob(os.path.join(self.sample_dir,"*/umi_count.txt"))
        merge_count_files(self.sample_dir,self.count_file,self.sample_name,True,len(self.cell_indices),files_to_merge)
        ## Join the files
        if config().seqtype.upper() == 'WTS':
            wts = True
        else:
            ## Merge primer level count files
            wts = False
            files_to_merge = glob.glob(os.path.join(self.sample_dir,"*/umi_count.primers.txt"))
            merge_count_files(self.sample_dir,self.count_file_primers,self.sample_name,wts,len(self.cell_indices),files_to_merge)            
        ## Merge metric files
        files_to_merge = glob.glob(os.path.join(self.sample_dir,"*/read_stats.txt"))
        merge_metric_files(self.sample_dir,self.temp_metric_file,self.metric_file,self.metric_file_cell,self.sample_name,wts,len(self.cell_indices),files_to_merge)
        with open(self.verification_file,'w') as OUT:
            print >> OUT,"verification"
        logger.info("Finished Task: {x}-{y} {z}".format(x='JoinCountFiles',y=self.sample_name,z=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
  
    def output(self):
        ''' Output from this task
        '''
        return luigi.LocalTarget(self.verification_file)

class CombineSamples(luigi.Task):
    ''' Task for combining results from multiple samples
    '''
    # Parameters
    output_dir = luigi.Parameter()
    samples_cfg = luigi.Parameter()
    cell_index_file = luigi.Parameter()
    vector_sequence = luigi.Parameter()
    isolator = luigi.Parameter()
    mt_len = luigi.IntParameter()
    num_cores = luigi.IntParameter()
    num_errors = luigi.IntParameter()

    def __init__(self,*args,**kwargs):
        ''' Class constructor
        '''
        super(CombineSamples,self).__init__(*args,**kwargs)
	self.runid = os.path.basename(self.output_dir)
        self.combined_count_file = os.path.join(self.output_dir,'{}.umi_counts.gene.{pcatn}.txt'.format(self.runid,config().catalog_number))
        self.combined_count_file_primers = os.path.join(self.output_dir,'{runid}.umi_counts.primer.{pcatn}.txt'.format(runid=self.runid,pcatn=config().catalog_number))        
        self.combined_cell_metrics_file = os.path.join(self.output_dir,'{}.metrics.by_cell_index.txt'.format(self.runid))
        self.combined_sample_metrics_file = os.path.join(self.output_dir,'{}.metrics.by_sample_index.txt'.format(self.runid))
        ## The verification file for this task
        self.target_dir = os.path.join(self.output_dir,'targets')
        if not os.path.exists(self.target_dir):
            os.makedirs(self.target_dir)
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.verification.txt')
        ## Annotation information from gencode
        if config().seqtype.upper() == 'WTS':
            global GENE_TREE
            GENE_TREE = create_gene_tree(config().annotation_gtf,config().ercc_bed)
        else:
            global GENE_HASH
            GENE_HASH = create_gene_hash(config().annotation_gtf,config().ercc_bed)
        
    def requires(self):
        ''' Task dependencies are joining sample count files
        '''
        dependencies = []
        parser = ConfigParser.ConfigParser()
        parser.read(self.samples_cfg)        
        for section in parser.sections():            
            sample_name = section
            R1_fastq = parser.get(section,'R1_fastq')
            R2_fastq = parser.get(section,'R2_fastq')
            instrument = parser.get(section,'Instrument')
            dependencies.append(
                JoinCountFiles(
                    R1_fastq=R1_fastq,R2_fastq=R2_fastq,
                    output_dir=self.output_dir,sample_name=sample_name,
                    cell_index_file=self.cell_index_file,vector_sequence=self.vector_sequence,
                    isolator=self.isolator,mt_len=self.mt_len,num_cores=self.num_cores,
                    num_errors=self.num_errors,instrument=instrument
                )
            )
        yield dependencies        

    def run(self):
        ''' Work to run is merging sample count and metric files
        '''
        logger.info("Started Task: {x} {y}".format(x='CombineSamples',y=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        ## Aggregate on gene level
        files_to_merge = glob.glob(os.path.join(self.output_dir,"*/*/umi_count.txt"))
        cells_to_restrict,cells_dropped,total_UMIs_genes = combine_count_files(files_to_merge,self.combined_count_file,True)
        ## Also, aggregate on primer level for targeted
        if config().seqtype.upper() != 'WTS':
            files_to_merge = glob.glob(os.path.join(self.output_dir,"*/*/umi_count.primers.txt"))
            cells_to_restrict,cells_dropped,total_UMIs_primers = combine_count_files(files_to_merge,self.combined_count_file_primers,False,cells_to_restrict)
        ## Aggregate metrics for cells
        files_to_merge = glob.glob(os.path.join(self.output_dir,"*/*_cell_stats.txt"))
        cell_metrics = combine_cell_metrics(files_to_merge,self.combined_cell_metrics_file,config().is_low_input,cells_to_restrict)
        ## Aggregate metrics across different samples
        files_to_merge = glob.glob(os.path.join(self.output_dir,"*/*_read_stats.txt"))
        sample_metrics = combine_sample_metrics(files_to_merge,self.combined_sample_metrics_file,config().is_low_input,cells_dropped,self.output_dir)
        ## Ensure metrics tally up between sample level and cell level files
        check_metric_counts(sample_metrics,cell_metrics,total_UMIs_genes)        
        
        ## Sort the UMI count files by gene/primer coordinates
        cmd1_gene = """ cat {count_file}| awk 'NR == 1; NR > 1 {{print $0 | "sort --ignore-case -V -k4,4 -k5,5 -k6,6"}}' > {temp}"""
        cmd1_primer = """ cat {count_file}| awk 'NR == 1; NR > 1 {{print $0 | "sort --ignore-case -V -k3,3 -k4,4 -k5,5"}}' > {temp}"""
        cmd2 = """ cp {temp} {count_file} """


        
	if config().seqtype.upper() != 'WTS':
            run_cmd(cmd1_primer.format(count_file=self.combined_count_file_primers,temp='temp.txt'))
            run_cmd(cmd2.format(temp='temp.txt',count_file=self.combined_count_file_primers))
        run_cmd(cmd1_gene.format(count_file=self.combined_count_file,temp='temp.txt'))
        run_cmd(cmd2.format(temp='temp.txt',count_file=self.combined_count_file))
        with open(self.verification_file,'w') as IN:
            IN.write('done\n')
	logger.info("Finished Task: {x} {y}".format(x='CombineSamples',y=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

    def output(self):
        ''' Output from this task
        '''
        return luigi.LocalTarget(self.verification_file)        
      
class ClusteringAnalysis(luigi.Task):
    ''' Task for carrying out secondary statistical analysis
    '''
    # Parameters
    output_dir = luigi.Parameter()
    samples_cfg = luigi.Parameter()
    cell_index_file = luigi.Parameter()
    vector_sequence = luigi.Parameter()
    isolator = luigi.Parameter()
    mt_len = luigi.IntParameter()
    num_cores = luigi.IntParameter()
    num_errors = luigi.IntParameter()

    def __init__(self,*args,**kwargs):
        ''' Class constructor
        '''
        super(ClusteringAnalysis,self).__init__(*args,**kwargs)
        ## Hard coded params specific to the R code
        self.ercc_file = '/home/qiauser/pipeline_data/expected_copy_for_ERCC.csv'
        self.niter = 500
        self.ncpu = 20
        self.k = 0
        self.perplexity = 10
        self.hvgthres = 0.40
        ## Generic Params for input files to the R code
        self.runid = os.path.basename(self.output_dir)
        self.combined_count_file = os.path.join(self.output_dir,'{}.umi_counts.gene.{pcatn}.txt'.format(self.runid,pcatn=config().catalog_number))
        self.combined_cell_metrics_file = os.path.join(self.output_dir,'{}.metrics.by_cell_index.txt'.format(self.runid))
        self.logfile = os.path.join(self.output_dir,'logs/')
        self.script_path_basics =  os.path.join(os.path.dirname(
            os.path.realpath(__file__)),'core/secondary_analysis_pipeline_BASiCS.R')
        self.script_path_scran =  os.path.join(os.path.dirname(
            os.path.realpath(__file__)),'core/secondary_analysis_pipeline_scran.R')        
        ## Pipeline specific params        
        self.target_dir = os.path.join(self.output_dir,'targets')
        if not os.path.exists(self.target_dir):
            os.makedirs(self.target_dir)
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.verification.txt')
        self.cmd_basics = (
            """ Rscript {script_path} {rundir} {count_file}.clean {ercc_file}"""
            """ {qc_file}.clean {runid} {niter} {ncpu} {k} {perplexity}"""
            """ {hvgthres} 2>&1""".format(
                script_path=self.script_path_basics,rundir=self.output_dir,
                count_file=self.combined_count_file,ercc_file=self.ercc_file,
                qc_file=self.combined_cell_metrics_file,runid=self.runid,
                niter=self.niter,ncpu=self.ncpu,k=self.k,
                perplexity=self.perplexity,hvgthres=self.hvgthres
            ))
        self.cmd_scran = (
            """ Rscript {script_path} {rundir} {count_file}.clean {ercc_file}"""
            """ {qc_file}.clean {runid} {ncpu} {k} {perplexity}"""
            """ {hvgthres} 2>&1""".format(
                script_path=self.script_path_scran,rundir=self.output_dir,
                count_file=self.combined_count_file,ercc_file=self.ercc_file,
                qc_file=self.combined_cell_metrics_file,runid=self.runid,
                ncpu=self.ncpu,k=self.k,
                perplexity=self.perplexity,hvgthres=self.hvgthres
            ))
        

    def requires(self):
        ''' Task dependends on successful completion of merging of all the 
        individual count files
        '''
        return self.clone(CombineSamples)

    def run(self):
        ''' Work to be done here is to run the R code
        '''
        logger.info("Starting Task: {x} {y}".format(x='ClusteringAnalysis',y=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        ## Clean the output files first
        clean_for_clustering(self.combined_cell_metrics_file,self.combined_count_file)
	## Run clustering analysis
        try:
            run_cmd(self.cmd_basics)
        except subprocess.CalledProcessError as e1:
            logger.info("Failed to run BASiCS based clustering : \n{e1}".format(e1=e1))
            if e1.returncode == 99: ## MCMC failed to converge
                try:
                    clustering_out = os.path.join(self.output_dir,'clustering_results')
                    misc_out = os.path.join(self.output_dir,'misc')
                    if os.path.exists(clustering_out):
                        run_cmd("mv {old} {new}".format(old=clustering_out,new=clustering_out+"_basics_failed"))
                    if os.path.exists(misc_out):
                        run_cmd("mv {old} {new}".format(old=misc_out,new=misc_out+"_basics_failed"))
                    run_cmd(self.cmd_scran)
                except Exception as e2:
                    raise(Exception(e2))
            else: ## Raise Exception if failed for reasons other than MCMC
                raise(Exception(e1))

        with open(self.verification_file,'w') as IN:
            IN.write('done\n')
        logger.info("Finished Task: {x} {y}".format(x='ClusteringAnalysis',y=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

    def output(self):
        ''' The output from this task to check is
        the verification file
        '''
        return luigi.LocalTarget(self.verification_file)


class WriteExcelSheet(luigi.Task):
    ''' Task for writing the metric and count files in a Excel workbook for the low input case
    '''
    # Parameters
    output_dir = luigi.Parameter()
    samples_cfg = luigi.Parameter()
    cell_index_file = luigi.Parameter()
    vector_sequence = luigi.Parameter()
    isolator = luigi.Parameter()
    mt_len = luigi.IntParameter()
    num_cores = luigi.IntParameter()
    num_errors = luigi.IntParameter()

    def __init__(self,*args,**kwargs):
        ''' Class constructor
        '''
        super(WriteExcelSheet,self).__init__(*args,**kwargs)
	self.runid = os.path.basename(self.output_dir)
        self.combined_count_file = os.path.join(self.output_dir,'{runid}.umi_counts.gene.{pcatn}.txt'.format(runid=self.runid,pcatn=config().catalog_number))
        self.combined_count_file_primers = os.path.join(self.output_dir,'{runid}.umi_counts.primer.{pcatn}.txt'.format(runid=self.runid,pcatn=config().catalog_number))        
        self.combined_cell_metrics_file = os.path.join(self.output_dir,'{}.metrics.by_cell_index.txt'.format(self.runid))
        self.combined_sample_metrics_file = os.path.join(self.output_dir,'{}.metrics.by_sample_index.txt'.format(self.runid))	
        self.combined_workbook = os.path.join(self.output_dir,'QIAseqUltraplexRNA_{}.xlsx'.format(self.runid))
        ## The verification file for this task
        self.target_dir = os.path.join(self.output_dir,'targets')
        if not os.path.exists(self.target_dir):
            os.makedirs(self.target_dir)
        self.verification_file = os.path.join(self.target_dir,
                                              self.__class__.__name__+
                                              '.verification.txt')
        if config().seqtype.upper() == 'WTS':
            self.files_to_write = [self.combined_sample_metrics_file,self.combined_cell_metrics_file,self.combined_count_file]
        else:
            self.files_to_write = [self.combined_sample_metrics_file,self.combined_cell_metrics_file,self.combined_count_file,self.combined_count_file_primers]
        

    def requires(self):
        ''' Task dependency is the joining of the count/metric files
        '''
        return self.clone(CombineSamples)

    def run(self):
        ''' Work to be done here is writing the excel workbook
        '''
        logger.info("Starting Task: {x} {y}".format(x='WriteExcelSheet',y=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))        
        if config().catalog_number == "N/A":
            catalog_number = None
        else:
            catalog_number = config().catalog_number
        write_excel_workbook(self.files_to_write,self.combined_workbook,catalog_number,config().species)
        with open(self.verification_file,'w') as IN:
            IN.write('done\n')
        logger.info("Finished Task: {x} {y}".format(x='WriteExcelSheet',y=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

        
    def output(self):
        ''' The output from this task is to check the verification file
        '''
        return luigi.LocalTarget(self.verification_file)
