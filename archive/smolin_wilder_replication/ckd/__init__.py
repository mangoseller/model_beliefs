"""Chronic-kidney-disease (CKD) screening domain (Methods; Appendix prompt catalog).

Stages, in order:

``download``  NHANES 2011-2018 XPT files from the CDC.
``nhanes``    pooled adult NHANES table with the CKD label (eGFR/UACR).
``risk``      cross-validated XGBoost CKD risk score used to stratify cases.
``cases``     the two risk-stratified 500-case cohorts.
``prompts``   the sixteen prompt families for the 500 analysed cases.
``parse``     strict parsing of the collected responses.
"""
