# scRNA-seq secondary data analysis 
# vim: tabstop=9 expandtab shiftwidth=3 softtabstop=3
# main script using BASiCS-1.1.0 for normalization and HVG detection
# Chang Xu, 11JAN2018 

# clear all R objects
rm(list=ls())

# load functions
source("/srv/qgen/code/qiaseq-singlecell-rna/core/functions.R")

#list of arguments
args <- commandArgs(TRUE)
wd <- args[1]
umi.counts <- args[2]
ercc.input <- args[3]
qc.metrics <- args[4]
run.id <- args[5]
n.iter <- args[6]
n.cpu <- args[7]
k.def <- args[8]
perplexity <- args[9]
hvg.thres <- args[10]

# create log file and record starting time
sink(paste0("./", run.id, ".log"))
writeLines("Parameters:")
writeLines(paste0("wd = ", wd))
writeLines(paste0("umi.counts = ", umi.counts))
writeLines(paste0("ercc.input = ", ercc.input))
writeLines(paste0("qc.metrics = ", qc.metrics))
writeLines(paste0("run.id = ", run.id))
writeLines(paste0("n.iter = ", n.iter))
writeLines(paste0("n.cpu = ", n.cpu))
writeLines(paste0("k.def = ", k.def))
writeLines(paste0("perplexity = ", perplexity))
writeLines(paste0("hvg.thres = ", hvg.thres, "\n"))

n.iter <- as.numeric(n.iter)
n.cpu <- as.numeric(n.cpu)
k.def <- as.numeric(k.def)
perplexity <- as.numeric(perplexity)
hvg.thres <- as.numeric(hvg.thres)

t0 <- Sys.time()
writeLines(paste0("Start time: ", t0))

# MCMC iteration must be multiply of 10 for thining purpose
if(n.iter %% 10 != 0) stop("n.iter must be multiply of 10!")

# load packages
suppressMessages(library(ggplot2))
suppressMessages(library(BASiCS))
suppressMessages(library(dplyr))
suppressMessages(library(tibble))
suppressMessages(library(Rtsne))
suppressMessages(library(scde))
suppressMessages(library(colorspace))
suppressMessages(library(cluster))
suppressMessages(library(pheatmap))
suppressMessages(library(edgeR))

options(stringsAsFactors=F)
select <- dplyr::select
filter <- dplyr::filter
rename <- dplyr::rename

# constants 
seed <- 8062017
n.trial <- 20
n.boot <- 500
writeLines(paste0("Random number seed: ", as.character(seed)))

if(file.exists(wd)){
  setwd(wd)
} else{
  stop(paste0(wd, " not found! Program stopped."))
}
set.seed(seed)

# make output directory
if(!file.exists('secondary_analysis')) dir.create('secondary_analysis')
if(!file.exists('misc')) dir.create('misc')

##############################################################################
# import and process raw data
##############################################################################
# read in raw data
if(file.exists(ercc.input)){
  ercc.orig <- read.csv(ercc.input, header=T, check.names=F)
  colnames(ercc.orig) <- c('ERCC_ID', 'input_2ul', 'input_1ul')
} else{
  stop(paste0(ercc.input, " not found! Program stopped."))
}

if(file.exists(umi.counts)){
  counts.orig <- read.table(umi.counts, header=T, sep="\t", check.names=F)
} else{
  stop(paste0(umi.counts, " not found! Program stopped."))
}

if(file.exists(qc.metrics)){
  qc.orig <- read.table(qc.metrics, header=T, sep="\t", check.names=F)
} else{
  stop(paste0(qc.metrics, " not found! Program stopped."))
}

# process umi counts; sum up primer level counts to get gene level counts
counts.orig %>% select(-c(gene_id, chrom, loc_5prime_grch38, loc_3prime_grch38, strand)) %>%
            group_by(gene) %>%
            summarise_all(sum) -> dat
n.cell <- ncol(dat) - 1
writeLines(paste0("Number of valid cell indices: ", as.character(n.cell)))

##############################################################################
# prepare datasets required by BASiCS
##############################################################################
# ERCC spike-in input information; drop spike-in's with < 1 expected copy
ercc.orig %>% select(ERCC_ID, input_1ul) %>%
   rename(SpikeID = ERCC_ID, SpikeInput = input_1ul) %>% 
   filter(SpikeInput >= 500) %>% 
   mutate(SpikeInput = round(SpikeInput)) -> SpikeInfo
SpikeInput <- SpikeInfo$SpikeInput

# gene-cell UMI counts matrix
Counts <- as.matrix(dat[,2:ncol(dat)])
class(Counts) <- 'numeric'
rownames(Counts) <- dat$gene

# remove un-used spike-in's 
Counts <- Counts[!grepl('ERCC', rownames(Counts)) | rownames(Counts) %in% SpikeInfo$SpikeID, ]

# vector to indicate endogenous genes vs ERCC spike-in
Tech <- ifelse(grepl('ERCC', rownames(Counts)), TRUE, FALSE)

##############################################################################
# quality check - % of UMIs on endogenous genes; RLE plot (for <= 96 cells); observed vs. expected ERCC 
##############################################################################
# plot total UMIs and % of biological gene UMIs for quality control
totalUMIs <- apply(Counts, 2, sum)
totalEndoUMIs <- apply(Counts[!Tech, ], 2, sum)
pctEndoUMIs <- totalEndoUMIs / totalUMIs
pctEndoUMIs.df <- data.frame(pctEndoUMIs)
p.pctEndoUMIs <- ggplot(pctEndoUMIs.df, aes(x=pctEndoUMIs)) + geom_density() + xlab('% of UMIs mapped to endogenous genes')
ggsave(paste0('misc/', run.id, '.pct_endogenous_gene_UMI.png'), dpi=300, height=7, width=7)

# relative log expression (RLE) plot for ERCC; make plot if <= 96 cells. TODO: what to do when n>96? 
if(n.cell <= 96){
   ercc <- Counts[Tech, ]
   medCnt <- apply(ercc, 1, median)
   f <- function(x) log(x[1:length(x)] / x[length(x)])
   logr <- t(apply(ercc[medCnt > 0,], 1, f))
   cnt <- as.vector(logr)
   cell_ID <- rep(colnames(logr), each=nrow(logr))
   data.frame(cnt, cell_ID) %>% mutate(cell_ID = factor(cell_ID)) -> df
   # sort by median RLE
   df.med <- df %>% group_by(cell_ID) %>% filter(cnt != Inf) %>% summarise(med.cnt = median(cnt, na.rm=T)) %>% arrange(desc(med.cnt)) 
   df %>% mutate(cell_ID = factor(cell_ID, levels=df.med$cell_ID)) %>% 
      ggplot(aes(x=cell_ID, y=cnt)) + geom_boxplot() + geom_hline(yintercept=0, linetype='dashed') +
      theme(axis.text.x = element_text(angle = 270, hjust = 1)) + ylab('Relative Log Expression (ERCC)') + xlab('Cell ID') -> p.rle
   ggsave(paste0('misc/', run.id, '.RLE_ERCC.png'), dpi=400, height=8.94, width=15)
}

# observed vs expected ERCC; averaged across all cells
#Counts.ercc <- Counts[Tech, ]
#mean.obs.ercc <- apply(Counts.ercc, 1, sum) / n.cell
#df.ercc <- data.frame(mean.obs.ercc, SpikeInput)
#p.scatter.ercc <- ggplot(df.ercc, aes(x=mean.obs.ercc,  y=SpikeInput)) + geom_point() + xlab('observed ERCC UMI per cell (average)') + ylab('expected ERCC UMI per cell')
#ggsave(paste0('secondary_analysis/', run.id, '.observed_vs_expected_UMI_ERCC.png'), dpi=300)

##############################################################################
# filter low quality cells 
##############################################################################
# drop low-quality cells with 1) endogenous gene reads below 5th percentile OR 2) % of endogenous reads below 5th percentile OR 3) detected genes below 5th percentile
qc <- mutate(qc.orig, Mapped_reads = reads_used_aligned_to_genome + reads_used_aligned_to_ERCC, 
                   pct_Map_to_genes = ifelse(Mapped_reads > 0, reads_used_aligned_to_genome / Mapped_reads, 0), 
                   keep = ifelse(reads_used_aligned_to_genome >= max(100, quantile(reads_used_aligned_to_genome, 0.05)) & 
                                 pct_Map_to_genes >= max(0.1, quantile(pct_Map_to_genes, 0.05)) & 
                                 detected_genes >= max(1, quantile(detected_genes, 0.05)), TRUE, FALSE)) 
cellsToDrop <- filter(qc, keep == FALSE)$Cell

# update counts
newCounts <- Counts[,!colnames(Counts) %in% cellsToDrop]

# final overall quality check 
final.check <- overall.check(newCounts,ercc.input)
newCounts <- final.check$new.table
cellsToDrop <- c(cellsToDrop, final.check$cell.drop)
genesToDrop <- final.check$gene.drop
cellsToKeep <- colnames(newCounts)
genesToKeep <- rownames(newCounts)

# update qc data
qc <- mutate(qc, keep = ifelse(Cells %in% cellsToDrop, FALSE, TRUE)) 
qc.drop <- filter(qc, !keep) %>% select(-keep) 

# update counts data
newTech <- ifelse(grepl('ERCC', rownames(newCounts)), TRUE, FALSE)
spikeInclude <- rownames(newCounts)[newTech]
newSpikeInfo <- SpikeInfo[SpikeInfo$SpikeID %in% spikeInclude,]
newSpikeInput <- newSpikeInfo$SpikeInput
FilterData <- newBASiCS_Data(Counts=newCounts, Tech=newTech, SpikeInfo=newSpikeInfo)

# write dropped cells and genes to file
write.csv(qc.drop, paste0('secondary_analysis/', run.id, '.step1_dropped_cells.csv'), row.names=F, quote=F)
writeLines(paste0("Low-quality cells dropped: ", as.character(length(cellsToDrop))))
writeLines(paste0("Cells for downstream analyses: ", as.character(length(cellsToKeep))))

geneDropped <- data.frame(genesToDrop)
colnames(geneDropped) <- 'Genes_dropped'
write.csv(geneDropped, paste0('secondary_analysis/', run.id, '.step1_dropped_genes.csv'), row.names=F, quote=F)
writeLines(paste0("Low-expression genes dropped: ", as.character(length(genesToDrop))))
writeLines(paste0("Genes for downstream analyses: ", as.character(length(genesToKeep))))

##############################################################################
# MCMC sampling to estimate model parameters
##############################################################################
MCMC_Output <- tryCatch(BASiCS_MCMC(FilterData, N=n.iter, Thin=5, Burn=n.iter/10, StoreChains=F, StoreDir='./misc', RunName=run.id, PrintProgress=T),
	         error=quit(save="no",status=99,runLast=FALSE),
		 warning=quit(save="no",status=99,runLast=FALSE)
	 	 )


save(MCMC_Output, file=paste0('misc/', run.id, '.MCMCoutput.RData'))

# temporary back-up for MCMC failure

# TODO: verify convergence
# TODO: back-up plan if MCMC fails or poor convergence

# visually check convergence afterwards. Need to do it on-the-fly
png(paste0('misc/', run.id, '.traceplot_gene1_cell1.png'), res=300, height=1500, width=1200)
par(mfrow=c(3,2))
plot(MCMC_Output, Param = "mu", Gene = 1, log = "y", cex.lab=1)
plot(MCMC_Output, Param = "delta", Gene = 1, cex.lab=1)
plot(MCMC_Output, Param = "phi", Cell = 1, cex.lab=1)
plot(MCMC_Output, Param = "s", Cell= 1, cex.lab=1)
plot(MCMC_Output, Param = "nu", Cell = 1, cex.lab=1)
plot(MCMC_Output, Param = "theta", Batch = 1, cex.lab=1)
dev.off()

# summarize model fit
MCMC_Summary <- Summary(MCMC_Output)

# variance decomposition; There seems to be a bug in BASiCS_VarianceDecomp() function; disable this temporarily until fixed
#VarDecomp <- BASiCS_VarianceDecomp(FilterData, MCMC_Output)
#write.csv(VarDecomp, paste0('secondary_analysis/', run.id, '.step3_variance_decomposition.csv'), row.names=F, quote=F)

# highly and lowly variable genes
DetectHVG <- BASiCS_DetectHVG(Chain = MCMC_Output, VarThreshold = hvg.thres, Plot = F)
# HVG defined as genes above threshold or top 20% biological variation percentage (minimum 10 genes), to avoid edge cases when only a few genes meet threshold
HVG1 <- DetectHVG$Table %>% filter(HVG)
HVG <- HVG1$GeneName

#var20p <- max(10, ceiling(0.2 * nrow(VarDecomp)))
#HVG2 <- VarDecomp$GeneNames[1:var20p] 
#HVG <- unique(c(HVG1, HVG2))
df.HVG <- data.frame(HVG)
write.csv(df.HVG, paste0('secondary_analysis/', run.id, '.step3_highly_variable_genes.csv'), row.names=F, quote=F)

# log-log plot of inter-cell CV vs. mean transcripts per cell
newCounts <- data.frame(assay(FilterData), row.names=rownames(assay(FilterData)))
meanUMI <- apply(newCounts, 1, mean)
CV <- apply(newCounts, 1, function(x) sd(x) / mean(x))
newCounts %>% mutate(meanUMI = meanUMI, CV = CV, 
                     geneType = factor(ifelse(grepl('ERCC', rownames(newCounts)), 'Spike-in', ifelse(rownames(newCounts) %in% HVG, 'HVG', 'Others')), levels=c('HVG', 'Spike-in', 'Others'))) %>% 
            ggplot(aes(x=log(meanUMI), y=log(CV), colour=geneType)) + geom_point() + theme_bw() + xlab('log(mean expression)') + ylab('log(CV)') +
                  geom_abline(slope=-0.5, intercept=0, linetype='dashed') + scale_color_manual(values=c('red', 'blue', 'gray')) -> p.cv_mean
ggsave(paste0('secondary_analysis/', run.id, '.step2_CV_vs_mean_expression.png'), dpi=300, height=7, width=7)

# normalized UMI counts
DenoisedCounts <- BASiCS_DenoisedCounts(Data=FilterData, Chain=MCMC_Output)
write.csv(DenoisedCounts, paste0('secondary_analysis/', run.id, '.step2_normalized_UMI.csv'), row.names=T, quote=F)

##############################################################################
# cell clustering and visualization based on HVG
##############################################################################
# log-transform the counts 
hvg.DenoisedCounts <- t(DenoisedCounts[rownames(DenoisedCounts) %in% HVG, ])
log.hvg.DenoisedCounts <- log(1 + hvg.DenoisedCounts)
n.pc <- min(c(20,dim(hvg.DenoisedCounts)))
writeLines(paste0("Number of principal components used: ", n.pc))

# PCA based on HVG and log-transformed counts
pca.res <- prcomp(log.hvg.DenoisedCounts, scale. = T, center=T)
top.pc <- pca.res$x[,1:n.pc]

# T-SNE based on HVG and log-transformed counts
# Update perplexity to match the R t-SNE function check
temp<-as.integer((nrow(log.hvg.DenoisedCounts)-2)/3)
perplexity = min(perplexity,temp)
sprintf("Updated Perplexity to : ",perplexity)
tsne.hvg <- Rtsne(log.hvg.DenoisedCounts, dims=2, pca=T, perplexity=perplexity, check_duplicates=F, theta=0.0, initial_dims=n.pc)
tsne.hvg.y <- data.frame(tsne.hvg$Y)

# set maximum number of clusters: at least 2, but not exceeding the minimum of 10 and n_cell / 10. 
k.max <- max(2, min(round(nrow(hvg.DenoisedCounts)/10), 10))
writeLines(paste0("Maximum number of cell populations: ", as.character(k.max)))

# select the optimal k using Gap statistics if not provided by user
if(k.def == 0){
   k.def <- find.k(top.pc = top.pc, kmax = k.max, n.trial = n.trial, n.boot = n.boot)
   writeLines(paste0("Default cell populations based on Gap statistics: ", k.def))
} else{
   writeLines(paste0("Default cell populations based on user input: ", k.def))
}

##########################################################
# perform k-means clustering and DE analysis
##########################################################
df.DenoisedCounts <- data.frame(DenoisedCounts[!grepl("ERCC", rownames(DenoisedCounts)), ], check.names=F)
df.DenoisedCounts <- apply(df.DenoisedCounts,2,function(x) {storage.mode(x) <- 'integer'; x}) 
# fit error models for each cell
min_size = min(50,nrow(df.DenoisedCounts))
sprintf("Updated min.size.entries to : %i",min_size)

## scde based error modelling
ret <- tryCatch(
	model.prior(df.DenoisedCounts,n.cpu,min_size),
	error=function(c){
	print("scde based modelling failed, using edgeR")
	print(c)
	return(list("NA","NA"))
	})

cells.model = ret[[1]]
o.ifm <- ret[[2]]
o.prior <- ret[[3]]
scde.success = ret[[4]]

if (scde.success == "NA"){ ## scde failed
  run.edgeR = TRUE
  # create a new SingleCellExperiment object
  basics <- newBASiCS_Data(Counts=df.DenoisedCounts, Tech=newTech, SpikeInfo=newSpikeInfo) 
} else{  
  run.edgeR = FALSE
}

for(k in 2:k.max){
   # create subdirectory to store outputs
   subdir <- ifelse(k==k.def, paste0("secondary_analysis/k_", k, "_default"), paste0("secondary_analysis/k_", k))
   if(!file.exists(subdir)) dir.create(subdir)

   # consistent colors in clustering plot and heatmap
   colors <- rainbow_hcl(k)

   # clustering
   cluster.res <- kmeans.cluster(top.pc = top.pc, tsne = tsne.hvg.y, nclust = k, subdir = subdir, run.id = run.id, col = colors)

   # heatmap 
   heat.cluster <- data.frame(cluster.res) %>% rename(cluster = cluster.res)
   heat.counts <- data.frame(log.hvg.DenoisedCounts, check.names=F)
   fig.name <- paste0(subdir, "/", run.id, ".heatmap.png")
   heatmap_wrapper(counts = heat.counts, cluster = heat.cluster, filename = fig.name, col=colors)

   if (run.edgeR == TRUE){
   # DE analysis with edgeR
   de.fit <- fit.de.edgeR(cluster.res,basics)
   }

   # pair-wise DE analysis; Based on ALL genes
   pairs <- combn(sort(unique(cluster.res)), 2)  # all pairs of clusters; each col is a pair
   for(i in 1:ncol(pairs)){
      pair <- pairs[,i]
      sub.table <- df.DenoisedCounts[, names(cluster.res[cluster.res %in% pair])]
      sub.cluster <- cluster.res[cluster.res %in% pair]

      if(run.edgeR == TRUE){
        # DE analysis with edgeR
	de.res <- pair.de.edgeR(k.pair,de.fit)
      } else {
        # DE analysis with SCDE
        sub.o.ifm <- o.ifm[rownames(o.ifm) %in% colnames(sub.table), ]
        sub.cluster.new <- factor(sub.cluster[names(sub.cluster) %in% cells.model])
        de.res <- pair.de(o.ifm = sub.o.ifm, o.prior = o.prior, norm.table = sub.table, cluster = sub.cluster.new, pair = pair, nclust = k, n.cpu = n.cpu)	
      }      
      outfile.name <- paste0(subdir, "/", run.id, ".", pair[1], "_vs_", pair[2], ".diff.exp.csv")
      write.csv(de.res, outfile.name, row.names=F, quote=F)
   }

   # one-vs-all data; based on ALL genes
   if(k >= 3){
      for(j in 1:k){
         new.cluster.res <- factor(ifelse(as.numeric(cluster.res) == j, j, 10000))
         names(new.cluster.res) <- names(cluster.res)
         pair <- c(j, 10000)
	 if(run.edgeR == TRUE){
	   # DE analysis with edgeR
	   de.fit <- fit.de.edgeR(new.cluster.res,basics)
	   de.res <- pair.de.edgeR(2,pair,de.fit) %>% mutate(Group_B = "Others")
	 } else {
	   # DE analysis with SCDE
           de.res <- pair.de(o.ifm = o.ifm, o.prior = o.prior, norm.table = df.DenoisedCounts, cluster = new.cluster.res, pair = pair, nclust = k, n.cpu = n.cpu) %>% mutate(Group_B = "Others")	   
	 }
         outfile.name <- paste0(subdir, "/", run.id, ".", j, "_vs_others.diff.exp.csv")
         write.csv(de.res, outfile.name, row.names=F, quote=F)
      }
   }
}

# print session info
sessionInfo()
# record end time 
writeLines(paste0("End time: ", Sys.time()))
sink()


