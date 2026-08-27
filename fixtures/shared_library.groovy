#!/usr/bin/env groovy
@Library('shared-lib@main') _

import com.example.BuildHelper

def notify(msg) {
    echo "notify: ${msg}"
}

node('linux') {
    stage('Checkout') {
        checkout scm
    }
    stage('Build') {
        sh 'make build'
        notify('build done')
    }
    stage('Publish') {
        def version = readFile('VERSION').trim()
        sh "./publish.sh ${version}"
    }
}
